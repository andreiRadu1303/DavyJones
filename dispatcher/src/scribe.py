"""Scribe agent — asynchronous execution report generator.

After the overseer's sub-agents complete their work, the Scribe receives a job
via a queue, spawns a lightweight Claude agent to synthesise a human-readable
report, and persists it as JSON in the state directory.  The plugin fetches
reports through the HTTP API.

The queue is fire-and-forget: callers enqueue jobs and never block.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

from src.config import (
    MAX_REPORTS,
    REPORTS_DIR,
    SCRIBE_MAX_TURNS,
    SCRIBE_TIMEOUT,
)
from src.container_runner import run_raw
from src.models import TaskResult
from src.plan_models import OverseerPlan, topological_levels

logger = logging.getLogger(__name__)


# ─── Job model ─────────────────────────────────────────────────


@dataclass
class ScribeJob:
    """All the data the Scribe needs to produce a report."""
    plan: OverseerPlan
    results: dict[str, TaskResult]
    source: str                          # "commit" | "direct"
    source_detail: dict = field(default_factory=dict)
    description: str = ""
    duration_seconds: float = 0.0


# ─── Report models (Pydantic — serialised to JSON) ────────────


class ReportTask(BaseModel):
    id: str
    description: str
    file_path: str
    status: str
    summary: Optional[str] = None        # Scribe's per-task summary
    error: Optional[str] = None
    depends_on: list[str] = []
    level: int = 0
    hit_max_turns: bool = False


class Report(BaseModel):
    id: str
    created_at: str
    source: str
    source_detail: dict = {}
    description: str
    duration_seconds: float
    status: str                          # "completed" | "failed"
    summary: Optional[str] = None        # Scribe's overall prose summary
    task_count: int = 0
    succeeded: int = 0
    failed: int = 0
    tasks: list[ReportTask] = []


# ─── Scribe prompt builder ─────────────────────────────────────


_OUTPUT_TRUNCATE = 3000  # chars per task output fed to the Scribe


def _build_scribe_prompt(job: ScribeJob) -> str:
    """Build the prompt that tells the Scribe what to summarise."""
    levels = topological_levels(job.plan)
    level_map: dict[str, int] = {}
    for level_idx, level_tasks in enumerate(levels):
        for t in level_tasks:
            level_map[t.id] = level_idx

    # Assemble per-task data
    task_sections: list[str] = []
    for t in job.plan.tasks:
        r = job.results.get(t.id)
        status = r.status if r else "unknown"
        output = (r.output_text or "")[:_OUTPUT_TRUNCATE] if r else ""
        error = r.error if r else None
        hit_max = r.hit_max_turns if r else False

        section = (
            f"### Task {t.id}: {t.description}\n"
            f"- File: {t.file_path}\n"
            f"- Status: {status}\n"
            f"- Level: {level_map.get(t.id, 0)}\n"
            f"- Dependencies: {', '.join(t.depends_on) or 'none'}\n"
            f"- Hit max turns: {hit_max}\n"
        )
        if error:
            section += f"- Error: {error}\n"
        if output:
            section += f"\nAgent output:\n```\n{output}\n```\n"
        task_sections.append(section)

    succeeded = sum(1 for r in job.results.values() if r.status == "completed")
    failed_count = sum(1 for r in job.results.values() if r.status == "failed")

    prompt = f"""\
You are the Scribe agent for DavyJones, a task-dispatch system.
Your job is to read the execution data below and produce a clear, structured
report summarising what happened.

## Execution Data

- **Source**: {job.source}
- **Description**: {job.description}
- **Duration**: {job.duration_seconds:.1f}s
- **Tasks**: {len(job.plan.tasks)} total, {succeeded} succeeded, {failed_count} failed

{chr(10).join(task_sections)}

## Instructions

Produce your output as a single JSON object (no markdown fences, no extra text).
Follow this schema exactly:

{{
  "summary": "<2-4 sentence prose summary of the overall execution: what was requested, what happened, and the outcome>",
  "tasks": [
    {{
      "id": "<task id>",
      "summary": "<1-2 sentence summary of what this task did or why it failed>"
    }}
  ]
}}

Rules:
- The top-level "summary" should be a human-readable paragraph, not bullet points.
- Each task "summary" should be concise — what the agent accomplished or the failure reason.
- Output ONLY the JSON object, nothing else.
"""
    return prompt


# ─── Report storage ────────────────────────────────────────────


_storage_lock = threading.Lock()


def _ensure_reports_dir() -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)


def _save_report(report: Report) -> None:
    """Write report JSON and update the index."""
    _ensure_reports_dir()

    with _storage_lock:
        # Write individual report file
        report_path = os.path.join(REPORTS_DIR, f"{report.id}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))

        # Update index
        index_path = os.path.join(REPORTS_DIR, "_index.json")
        index: list[dict] = []
        if os.path.isfile(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    index = json.load(f)
            except (json.JSONDecodeError, OSError):
                index = []

        entry = {
            "id": report.id,
            "created_at": report.created_at,
            "source": report.source,
            "status": report.status,
            "description": report.description,
            "task_count": report.task_count,
            "succeeded": report.succeeded,
            "failed": report.failed,
        }
        index.insert(0, entry)

        # Prune old entries
        if len(index) > MAX_REPORTS:
            removed = index[MAX_REPORTS:]
            index = index[:MAX_REPORTS]
            for old in removed:
                old_path = os.path.join(REPORTS_DIR, f"{old['id']}.json")
                try:
                    os.remove(old_path)
                except OSError:
                    pass

        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)


def list_reports(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return report summaries from the index."""
    index_path = os.path.join(REPORTS_DIR, "_index.json")
    if not os.path.isfile(index_path):
        return []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        return index[offset:offset + limit]
    except (json.JSONDecodeError, OSError):
        return []


def get_report(report_id: str) -> dict | None:
    """Return full report data or None."""
    # Sanitise to prevent path traversal
    safe_id = os.path.basename(report_id)
    report_path = os.path.join(REPORTS_DIR, f"{safe_id}.json")
    if not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ─── Scribe worker ─────────────────────────────────────────────


_scribe_queue: queue.Queue[ScribeJob] = queue.Queue()


def _build_report_from_job(job: ScribeJob, scribe_output: dict | None) -> Report:
    """Assemble a Report, merging Scribe LLM output with raw execution data."""
    levels = topological_levels(job.plan)
    level_map: dict[str, int] = {}
    for level_idx, level_tasks in enumerate(levels):
        for t in level_tasks:
            level_map[t.id] = level_idx

    # Build per-task summaries from Scribe output
    task_summaries: dict[str, str] = {}
    if scribe_output and "tasks" in scribe_output:
        for ts in scribe_output["tasks"]:
            if isinstance(ts, dict) and "id" in ts:
                task_summaries[ts["id"]] = ts.get("summary", "")

    report_tasks: list[ReportTask] = []
    for t in job.plan.tasks:
        r = job.results.get(t.id)
        report_tasks.append(ReportTask(
            id=t.id,
            description=t.description,
            file_path=t.file_path,
            status=r.status if r else "unknown",
            summary=task_summaries.get(t.id, (r.output_text or "")[:500] if r else None),
            error=r.error if r else None,
            depends_on=t.depends_on,
            level=level_map.get(t.id, 0),
            hit_max_turns=r.hit_max_turns if r else False,
        ))

    succeeded = sum(1 for r in job.results.values() if r.status == "completed")
    failed_count = sum(1 for r in job.results.values() if r.status == "failed")
    overall_status = "completed" if failed_count == 0 else "failed"

    return Report(
        id=f"rpt-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc).isoformat(),
        source=job.source,
        source_detail=job.source_detail,
        description=job.description,
        duration_seconds=job.duration_seconds,
        status=overall_status,
        summary=scribe_output.get("summary") if scribe_output else None,
        task_count=len(job.plan.tasks),
        succeeded=succeeded,
        failed=failed_count,
        tasks=report_tasks,
    )


def _process_job(job: ScribeJob) -> None:
    """Process a single scribe job: run Claude, parse output, save report."""
    logger.info("Scribe processing job: %s", job.description[:100])

    scribe_output: dict | None = None

    try:
        prompt = _build_scribe_prompt(job)
        exit_code, stdout, stderr = run_raw(
            prompt=prompt,
            max_turns=SCRIBE_MAX_TURNS,
            timeout=SCRIBE_TIMEOUT,
        )

        if exit_code == 0:
            # Parse Claude CLI wrapper → extract result → parse JSON
            raw_text = stdout
            try:
                envelope = json.loads(stdout)
                if isinstance(envelope, dict) and "result" in envelope:
                    raw_text = envelope["result"]
            except json.JSONDecodeError:
                pass

            # The Scribe should output raw JSON, try to parse it
            try:
                scribe_output = json.loads(raw_text)
            except json.JSONDecodeError:
                # Try extracting JSON from markdown fences
                import re
                match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
                if match:
                    try:
                        scribe_output = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        pass

            if scribe_output is None:
                logger.warning("Scribe output was not valid JSON, using fallback")
        else:
            logger.warning("Scribe agent failed (exit=%d), using fallback report", exit_code)
    except Exception:
        logger.exception("Scribe agent crashed, using fallback report")

    # Build and save report (with or without Scribe prose)
    report = _build_report_from_job(job, scribe_output)
    _save_report(report)
    logger.info("Scribe report saved: %s (%s)", report.id, report.status)


def _scribe_worker() -> None:
    """Background worker: consume jobs from the queue forever."""
    while True:
        try:
            job = _scribe_queue.get()
            _process_job(job)
        except Exception:
            logger.exception("Scribe worker error (continuing)")
        finally:
            _scribe_queue.task_done()


# ─── Public API ────────────────────────────────────────────────


def enqueue(job: ScribeJob) -> None:
    """Add a job to the Scribe queue (non-blocking)."""
    _scribe_queue.put_nowait(job)
    logger.info("Scribe job enqueued: %s", job.description[:100])


def start() -> None:
    """Start the Scribe background worker thread."""
    thread = threading.Thread(
        target=_scribe_worker,
        daemon=True,
        name="scribe-worker",
    )
    thread.start()
    logger.info("Scribe worker started")
