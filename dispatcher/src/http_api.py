"""HTTP API for direct task submission from the Obsidian plugin.

Runs Flask in a daemon thread (same pattern as SlackListener and GitHubMonitor).
Exposes POST /api/task for submitting tasks and GET /api/task/<id> for status.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, jsonify, request

from src.config import (
    HTTP_PORT,
    OVERSEER_MAX_TURNS,
    OVERSEER_TIMEOUT_SECONDS,
    VAULT_PATH,
)
import time

from src.container_runner import run_raw
from src.context_resolver import resolve, resolve_batch
from src.overseer import execute_plan, extract_plan
from src.overseer_prompt import build_direct_task
from src.plan_models import OverseerPlan, validate_plan
from src.scribe import ScribeJob, enqueue as scribe_enqueue, get_report, list_reports
from src.vault_rules import load_vault_rules

logger = logging.getLogger(__name__)


# ─── Task tracking ─────────────────────────────────────────────


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
) -> None:
    """Background thread: resolve context, run overseer, execute plan."""
    try:
        task.status = "running"
        logger.info("Direct task %s started: %s", task.id, description[:100])

        vault_rules = load_vault_rules()

        # Resolve context for scope files
        if scope_files:
            scope_context = resolve_batch(VAULT_PATH, scope_files)
        else:
            scope_context = ""

        prompt = build_direct_task(
            description=description,
            scope_files=scope_files,
            scope_context=scope_context,
            vault_rules=vault_rules,
        )

        logger.info("Direct task %s: running overseer", task.id)
        exit_code, stdout, stderr = run_raw(
            prompt=prompt,
            max_turns=OVERSEER_MAX_TURNS,
            timeout=OVERSEER_TIMEOUT_SECONDS,
        )

        if exit_code != 0:
            task.status = "failed"
            task.error = (stderr or stdout)[:500]
            logger.error("Direct task %s: overseer failed (exit=%d): %s",
                         task.id, exit_code, task.error)
            return

        plan_data = extract_plan(stdout)
        if plan_data is None:
            task.status = "failed"
            task.error = "Could not parse overseer plan from output"
            logger.error("Direct task %s: %s", task.id, task.error)
            return

        plan = OverseerPlan(**plan_data)
        errors = validate_plan(plan)
        if errors:
            task.status = "failed"
            task.error = f"Invalid plan: {errors}"
            logger.error("Direct task %s: %s", task.id, task.error)
            return

        if not plan.tasks:
            task.status = "completed"
            logger.info("Direct task %s: overseer returned empty plan", task.id)
            return

        task.task_count = len(plan.tasks)
        logger.info("Direct task %s: executing plan with %d tasks", task.id, task.task_count)

        t_start = time.time()
        results = execute_plan(plan, auto_commit_fn)
        duration = time.time() - t_start

        task.succeeded = sum(1 for r in results.values() if r.status == "completed")
        task.failed = sum(1 for r in results.values() if r.status == "failed")
        task.status = "completed" if task.failed == 0 else "failed"
        if task.failed > 0:
            task.error = f"{task.failed}/{task.task_count} sub-tasks failed"

        logger.info("Direct task %s finished: %d/%d succeeded",
                     task.id, task.succeeded, task.task_count)

        # Enqueue Scribe report (fire-and-forget)
        try:
            scribe_enqueue(ScribeJob(
                plan=plan,
                results=results,
                source="direct",
                source_detail={"task_id": task.id},
                description=description[:200],
                duration_seconds=duration,
            ))
        except Exception:
            logger.exception("Failed to enqueue Scribe job for direct task %s", task.id)

    except Exception as e:
        logger.exception("Direct task %s crashed", task.id)
        task.status = "failed"
        task.error = str(e)


# ─── Flask app ──────────────────────────────────────────────────


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
        return jsonify({
            "task_id": task.id,
            "status": task.status,
            "description": task.description,
            "error": task.error,
            "task_count": task.task_count,
            "succeeded": task.succeeded,
            "failed": task.failed,
            "report_id": task.report_id,
        })

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
