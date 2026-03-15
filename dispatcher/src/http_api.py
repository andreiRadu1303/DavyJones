"""HTTP API for direct task submission from the Obsidian plugin.

Runs Flask in a daemon thread (same pattern as SlackListener and GitHubMonitor).
Exposes POST /api/task for submitting tasks and GET /api/task/<id> for status.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request

from src.config import (
    HTTP_PORT,
    OVERSEER_MAX_TURNS,
    OVERSEER_TIMEOUT_SECONDS,
    VAULT_PATH,
)
from src.container_runner import run_raw_streaming
from src.context_resolver import resolve, resolve_batch
from src.overseer import execute_plan, extract_plan
from src.overseer_prompt import build_direct_task
from src.plan_models import OverseerPlan, topological_levels, validate_plan
from src.scribe import ScribeJob, enqueue as scribe_enqueue, get_report, list_reports
from src.vault_rules import load_vault_rules

logger = logging.getLogger(__name__)


# ─── Task tracking ─────────────────────────────────────────────


_OUTPUT_TAIL_MAX = 4000  # max chars kept per subtask output tail


@dataclass
class SubtaskProgress:
    """Live progress for a single sub-task in the plan."""
    id: str
    description: str
    file_path: str
    level: int = 0
    status: str = "pending"  # pending | running | completed | failed
    output: str = ""         # rolling tail of agent output (thinking / tool use)
    execution_log: str = ""  # full execution log after completion
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class DirectTask:
    """Tracks a direct task through its lifecycle."""
    id: str
    status: str = "queued"  # queued | running | completed | failed
    description: str = ""
    error: Optional[str] = None
    task_count: int = 0
    succeeded: int = 0
    failed: int = 0
    report_id: Optional[str] = None
    # Live progress fields
    phase: str = "queued"  # queued | planning | executing | reporting | done
    overseer_output: str = ""
    overseer_execution_log: str = ""
    subtasks: list[SubtaskProgress] = field(default_factory=list)
    plan_json: Optional[dict] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


_tasks: dict[str, DirectTask] = {}
_tasks_lock = threading.Lock()
_MAX_TASKS = 100


def _store_task(task: DirectTask) -> None:
    with _tasks_lock:
        _tasks[task.id] = task
        if len(_tasks) > _MAX_TASKS:
            oldest_key = next(iter(_tasks))
            del _tasks[oldest_key]


def _get_task(task_id: str) -> DirectTask | None:
    with _tasks_lock:
        return _tasks.get(task_id)


# ─── Task execution ────────────────────────────────────────────


def _execute_direct_task(
    task: DirectTask,
    description: str,
    scope_files: list[str],
    auto_commit_fn,
    source: str = "direct",
    source_detail: dict | None = None,
) -> None:
    """Background thread: resolve context, run overseer, execute plan."""
    try:
        task.status = "running"
        task.started_at = datetime.now(timezone.utc).isoformat()
        logger.info("Direct task %s started: %s", task.id, description[:100])

        vault_rules = load_vault_rules()

        # ── Phase: planning ──────────────────────────────────────
        task.phase = "planning"

        # Load trigger config
        triggers = vault_rules.get("triggers", [])
        trigger_depth = vault_rules.get("triggerDepth", 1)
        trigger_max_files = vault_rules.get("triggerMaxFiles", 30)
        hierarchy_depth = vault_rules.get("hierarchyDepth", 2)

        if scope_files:
            scope_context = resolve_batch(
                VAULT_PATH, scope_files,
                triggers=triggers,
                trigger_depth=trigger_depth,
                trigger_max_files=trigger_max_files,
                hierarchy_depth=hierarchy_depth,
            )
        else:
            scope_context = ""

        prompt = build_direct_task(
            description=description,
            scope_files=scope_files,
            scope_context=scope_context,
            vault_rules=vault_rules,
        )

        logger.info("Direct task %s: running overseer", task.id)

        def _on_overseer_output(chunk: str):
            task.overseer_output = (task.overseer_output + chunk)[-_OUTPUT_TAIL_MAX:]

        exit_code, result_json, execution_log = run_raw_streaming(
            prompt=prompt,
            max_turns=OVERSEER_MAX_TURNS,
            timeout=OVERSEER_TIMEOUT_SECONDS,
            on_output=_on_overseer_output,
        )

        task.overseer_execution_log = execution_log or ""

        if exit_code != 0:
            task.status = "failed"
            task.phase = "done"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            task.error = (execution_log or result_json)[:500]
            logger.error("Direct task %s: overseer failed (exit=%d): %s",
                         task.id, exit_code, task.error)
            return

        plan_data = extract_plan(result_json)
        if plan_data is None:
            task.status = "failed"
            task.phase = "done"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            task.error = "Could not parse overseer plan from output"
            logger.error("Direct task %s: %s", task.id, task.error)
            return

        plan = OverseerPlan(**plan_data)
        errors = validate_plan(plan)
        if errors:
            task.status = "failed"
            task.phase = "done"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            task.error = f"Invalid plan: {errors}"
            logger.error("Direct task %s: %s", task.id, task.error)
            return

        if not plan.tasks:
            task.status = "completed"
            task.phase = "done"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            logger.info("Direct task %s: overseer returned empty plan", task.id)
            return

        # ── Phase: executing ─────────────────────────────────────
        task.phase = "executing"
        task.plan_json = plan_data
        task.task_count = len(plan.tasks)

        # Populate subtask progress from plan
        levels = topological_levels(plan)
        level_map: dict[str, int] = {}
        for level_idx, level_tasks in enumerate(levels):
            for t in level_tasks:
                level_map[t.id] = level_idx

        task.subtasks = [
            SubtaskProgress(
                id=t.id,
                description=t.description,
                file_path=t.file_path,
                level=level_map.get(t.id, 0),
            )
            for t in plan.tasks
        ]

        logger.info("Direct task %s: executing plan with %d tasks", task.id, task.task_count)

        # Callbacks to update subtask progress in real-time
        def _on_task_start(task_id: str):
            for st in task.subtasks:
                if st.id == task_id:
                    st.status = "running"
                    st.started_at = datetime.now(timezone.utc).isoformat()
                    break

        def _on_task_finish(task_id: str, result):
            for st in task.subtasks:
                if st.id == task_id:
                    st.status = result.status
                    st.error = result.error
                    st.execution_log = getattr(result, 'execution_log', '') or ''
                    st.finished_at = datetime.now(timezone.utc).isoformat()
                    break
            task.succeeded = sum(1 for s in task.subtasks if s.status == "completed")
            task.failed = sum(1 for s in task.subtasks if s.status == "failed")

        def _on_task_output(task_id: str, chunk: str):
            for st in task.subtasks:
                if st.id == task_id:
                    st.output = (st.output + chunk)[-_OUTPUT_TAIL_MAX:]
                    break

        t_start = time.time()
        results = execute_plan(
            plan, auto_commit_fn,
            on_task_start=_on_task_start,
            on_task_finish=_on_task_finish,
            on_task_output=_on_task_output,
        )
        duration = time.time() - t_start

        task.succeeded = sum(1 for r in results.values() if r.status == "completed")
        task.failed = sum(1 for r in results.values() if r.status == "failed")
        task.status = "completed" if task.failed == 0 else "failed"
        if task.failed > 0:
            task.error = f"{task.failed}/{task.task_count} sub-tasks failed"

        logger.info("Direct task %s finished: %d/%d succeeded",
                     task.id, task.succeeded, task.task_count)

        # Auto-commit any files the agents changed
        try:
            auto_commit_fn(f"direct-task/{task.id}", task.status)
        except Exception:
            logger.exception("Auto-commit failed for direct task %s", task.id)

        # ── Phase: reporting ─────────────────────────────────────
        task.phase = "reporting"
        task.finished_at = datetime.now(timezone.utc).isoformat()

        try:
            scribe_enqueue(ScribeJob(
                plan=plan,
                results=results,
                source=source,
                source_detail=source_detail or {"task_id": task.id},
                description=description[:200],
                duration_seconds=duration,
                overseer_raw_output=task.overseer_output,
                plan_json=plan_data,
            ))
        except Exception:
            logger.exception("Failed to enqueue Scribe job for direct task %s", task.id)

        task.phase = "done"

    except Exception as e:
        logger.exception("Direct task %s crashed", task.id)
        task.status = "failed"
        task.phase = "done"
        task.finished_at = datetime.now(timezone.utc).isoformat()
        task.error = str(e)


# ─── Flask app ──────────────────────────────────────────────────


def _serialize_task(task: DirectTask) -> dict:
    """Serialize a DirectTask for API responses, including live progress."""
    return {
        "task_id": task.id,
        "status": task.status,
        "phase": task.phase,
        "description": task.description,
        "error": task.error,
        "task_count": task.task_count,
        "succeeded": task.succeeded,
        "failed": task.failed,
        "report_id": task.report_id,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "overseer_output": task.overseer_output[-3000:] if task.overseer_output else "",
        "overseer_execution_log": task.overseer_execution_log,
        "plan_json": task.plan_json,
        "subtasks": [
            {
                "id": st.id,
                "description": st.description,
                "file_path": st.file_path,
                "level": st.level,
                "status": st.status,
                "output": st.output,
                "execution_log": st.execution_log,
                "error": st.error,
                "started_at": st.started_at,
                "finished_at": st.finished_at,
            }
            for st in task.subtasks
        ],
    }


def create_app(auto_commit_fn) -> Flask:
    """Create the Flask app with all routes."""
    app = Flask(__name__)

    @app.after_request
    def _cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.route("/api/task", methods=["POST"])
    def submit_task():
        data = request.get_json(silent=True) or {}

        description = data.get("description", "").strip()
        if not description:
            return jsonify({"error": "description is required"}), 400

        scope_files = data.get("scopeFiles", [])
        if not isinstance(scope_files, list):
            scope_files = []

        task = DirectTask(
            id=str(uuid.uuid4())[:8],
            description=description[:500],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _store_task(task)

        t = threading.Thread(
            target=_execute_direct_task,
            args=(task, description, scope_files, auto_commit_fn),
            daemon=True,
            name=f"direct-task-{task.id}",
        )
        t.start()

        logger.info("Direct task %s queued: %s", task.id, description[:100])
        return jsonify({"task_id": task.id, "status": "queued"}), 202

    @app.route("/api/task/<task_id>", methods=["GET"])
    def get_task_status(task_id):
        task = _get_task(task_id)
        if not task:
            return jsonify({"error": "not found"}), 404
        return jsonify(_serialize_task(task))

    @app.route("/api/tasks/active", methods=["GET"])
    def get_active_tasks():
        with _tasks_lock:
            active = [t for t in _tasks.values()
                       if t.status in ("queued", "running")]
        return jsonify({
            "tasks": [_serialize_task(t) for t in active],
        })

    @app.route("/api/claude-changes", methods=["GET"])
    def get_claude_changes():
        from src.claude_changes import get_changed_files
        return jsonify({"files": get_changed_files()})

    @app.route("/api/claude-changes/clear", methods=["POST"])
    def clear_claude_changes():
        from src.claude_changes import clear_changed_files
        clear_changed_files()
        return jsonify({"status": "cleared"})

    @app.route("/api/reports", methods=["GET"])
    def list_reports_endpoint():
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        reports = list_reports(limit=min(limit, 100), offset=offset)
        return jsonify({"reports": reports})

    @app.route("/api/reports/<report_id>", methods=["GET"])
    def get_report_endpoint(report_id):
        report = get_report(report_id)
        if not report:
            return jsonify({"error": "not found"}), 404
        return jsonify(report)

    @app.route("/api/calendar", methods=["GET"])
    def get_calendar():
        cal_path = os.path.join(VAULT_PATH, ".davyjones-calendar.json")
        try:
            with open(cal_path, "r") as f:
                data = json.load(f)
            return jsonify(data)
        except FileNotFoundError:
            return jsonify({"version": 1, "calendars": [], "events": []})
        except json.JSONDecodeError:
            return jsonify({"error": "invalid calendar file"}), 500

    @app.route("/api/calendar/event", methods=["POST"])
    def create_calendar_event():
        data = request.get_json(silent=True) or {}
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400

        start = data.get("start", "")
        if not start:
            return jsonify({"error": "start is required"}), 400

        # Generate unique ID
        event_id = "evt-" + uuid.uuid4().hex[:8]
        end = data.get("end", start)
        all_day = data.get("allDay", False)
        event_type = data.get("type", "event")
        task_config = data.get("task", None)

        event = {
            "id": event_id,
            "calendarId": data.get("calendarId", "default"),
            "title": title,
            "description": data.get("description", ""),
            "start": start,
            "end": end,
            "allDay": all_day,
            "color": data.get("color", None),
            "type": event_type,
            "recurrence": data.get("recurrence", None),
            "task": task_config,
        }

        cal_path = os.path.join(VAULT_PATH, ".davyjones-calendar.json")
        try:
            with open(cal_path, "r") as f:
                cal_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cal_data = {
                "version": 1,
                "calendars": [{"id": "default", "name": "Default", "color": "#7c3aed", "source": "local"}],
                "events": [],
            }

        cal_data.setdefault("events", []).append(event)

        with open(cal_path, "w") as f:
            json.dump(cal_data, f, indent=2)

        logger.info("Calendar event created via API: %s (%s)", event_id, title)
        return jsonify({"event_id": event_id, "status": "created"}), 201

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    return app


# ─── Server class ───────────────────────────────────────────────


class HttpApi:
    """HTTP API server running in a daemon thread."""

    def __init__(self, auto_commit_fn) -> None:
        self.auto_commit_fn = auto_commit_fn
        self.app = create_app(auto_commit_fn)

    def start(self) -> None:
        thread = threading.Thread(
            target=self.app.run,
            kwargs={"host": "0.0.0.0", "port": HTTP_PORT, "debug": False},
            daemon=True,
            name="http-api",
        )
        thread.start()
        logger.info("HTTP API started on port %d", HTTP_PORT)
