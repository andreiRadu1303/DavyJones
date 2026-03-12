"""Calendar scheduler — dispatches task-type events at their scheduled time.

Background daemon thread that polls `.davyjones-calendar.json` every 60 seconds,
detects task-type events whose start time has arrived, and dispatches them through
the same overseer pipeline used by the HTTP API direct tasks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from src.config import VAULT_PATH

logger = logging.getLogger(__name__)

CALENDAR_FILE = os.path.join(VAULT_PATH, ".davyjones-calendar.json")
POLL_INTERVAL = 60  # seconds
DISPATCH_WINDOW = timedelta(minutes=2)

# In-memory set to avoid double-dispatch within this process lifetime
_dispatched: set[str] = set()
_lock = threading.Lock()
_auto_commit_fn = None


def _read_calendar() -> dict:
    """Read the calendar JSON file, returning empty structure on failure."""
    try:
        with open(CALENDAR_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "calendars": [], "events": []}


def _write_calendar(data: dict) -> None:
    """Write the calendar JSON file."""
    try:
        with open(CALENDAR_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.exception("Failed to write calendar file")


def _expand_recurrence_next(event: dict, now: datetime) -> list[datetime]:
    """Return occurrence start times within the dispatch window.

    Only returns the *current* occurrence (if within window).
    """
    rec = event.get("recurrence")
    start_str = event.get("start", "")

    try:
        if "T" in start_str:
            base = datetime.fromisoformat(start_str)
        else:
            base = datetime.fromisoformat(start_str + "T00:00:00")
    except (ValueError, TypeError):
        return []

    if not rec:
        # Non-recurring: check if start is within the dispatch window
        if base <= now and (now - base) <= DISPATCH_WINDOW:
            return [base]
        return []

    freq = rec.get("freq", "daily")
    interval = rec.get("interval", 1) or 1
    until_str = rec.get("until")
    count = rec.get("count")
    by_day = rec.get("byDay", [])

    until = None
    if until_str:
        try:
            until = datetime.fromisoformat(
                until_str if "T" in until_str else until_str + "T23:59:59"
            )
        except (ValueError, TypeError):
            pass

    day_map = {"SU": 6, "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5}

    occurrences: list[datetime] = []
    occ = base
    occ_count = 0
    max_iter = 5000

    for _ in range(max_iter):
        if until and occ > until:
            break
        if count and occ_count >= count:
            break
        if occ > now + DISPATCH_WINDOW:
            break

        should_include = True
        if freq == "weekly" and by_day:
            # Python: Monday=0, Sunday=6
            occ_day_name = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][occ.weekday()]
            if occ_day_name not in by_day:
                should_include = False

        if should_include:
            if occ <= now and (now - occ) <= DISPATCH_WINDOW:
                occurrences.append(occ)
            occ_count += 1

        # Advance to next occurrence
        if freq == "daily":
            occ = occ + timedelta(days=interval)
        elif freq == "weekly":
            if by_day:
                occ = occ + timedelta(days=1)
            else:
                occ = occ + timedelta(weeks=interval)
        elif freq == "monthly":
            month = occ.month + interval
            year = occ.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            day = min(occ.day, 28)
            occ = occ.replace(year=year, month=month, day=day)
        elif freq == "yearly":
            try:
                occ = occ.replace(year=occ.year + interval)
            except ValueError:
                occ = occ.replace(year=occ.year + interval, day=28)
        else:
            break

    return occurrences


def _dispatch_instance_key(event_id: str, occ_time: datetime) -> str:
    """Unique key for a dispatched occurrence."""
    return f"{event_id}@{occ_time.isoformat()}"


def _dispatch_event(event: dict, occ_time: datetime) -> None:
    """Dispatch a single task-type event occurrence."""
    task_config = event.get("task", {}) or {}
    description = task_config.get("agentDescription", "")
    if not description:
        description = f"Calendar task: {event.get('title', 'Untitled')}"

    scope_files = task_config.get("scopeFiles", [])
    max_turns = task_config.get("maxTurns", 20) or 20

    logger.info(
        "Calendar dispatch: event=%s title=%r occ=%s",
        event.get("id", "?"),
        event.get("title", "?"),
        occ_time.isoformat(),
    )

    try:
        # Import here to avoid circular imports
        from src.http_api import DirectTask, _execute_direct_task, _store_task

        task = DirectTask(
            id=str(uuid.uuid4())[:8],
            description=f"[Calendar] {event.get('title', '')}",
        )
        _store_task(task)

        t = threading.Thread(
            target=_calendar_task_wrapper,
            args=(task, description, scope_files, event, occ_time),
            daemon=True,
            name=f"cal-task-{task.id}",
        )
        t.start()
    except Exception:
        logger.exception(
            "Failed to dispatch calendar event %s", event.get("id", "?")
        )


def _calendar_task_wrapper(
    task, description: str, scope_files: list, event: dict, occ_time: datetime
) -> None:
    """Wrapper that runs the task and writes status back to calendar JSON."""
    from src.http_api import _execute_direct_task

    _execute_direct_task(
        task,
        description,
        scope_files,
        _auto_commit_fn,
        source="calendar",
        source_detail={
            "task_id": task.id,
            "event_id": event.get("id", ""),
            "event_title": event.get("title", ""),
            "occurrence": occ_time.isoformat(),
        },
    )

    # Write dispatch result back to calendar JSON
    try:
        cal_data = _read_calendar()
        for ev in cal_data.get("events", []):
            if ev.get("id") == event.get("id") and ev.get("task"):
                ev["task"]["lastStatus"] = task.status
                ev["task"]["lastError"] = task.error
                break
        _write_calendar(cal_data)
    except Exception:
        logger.exception("Failed to write task status back to calendar")


def _poll_once() -> None:
    """Single poll iteration: check for due task events and dispatch."""
    now = datetime.now()
    cal_data = _read_calendar()
    events = cal_data.get("events", [])
    updated = False

    for event in events:
        if event.get("type") != "task":
            continue

        occurrences = _expand_recurrence_next(event, now)
        for occ_time in occurrences:
            key = _dispatch_instance_key(event.get("id", ""), occ_time)

            # Check persistent dispatched list
            dispatched_list = (event.get("task") or {}).get(
                "dispatchedInstances", []
            )
            if key in dispatched_list:
                continue

            # Check in-memory dedup
            with _lock:
                if key in _dispatched:
                    continue
                _dispatched.add(key)

            # Dispatch the event
            _dispatch_event(event, occ_time)

            # Record in the calendar JSON
            if event.get("task") is None:
                event["task"] = {}
            event["task"]["lastDispatchedAt"] = now.isoformat()
            if "dispatchedInstances" not in event["task"]:
                event["task"]["dispatchedInstances"] = []
            event["task"]["dispatchedInstances"].append(key)
            updated = True

    if updated:
        _write_calendar(cal_data)


def _loop() -> None:
    """Background loop."""
    logger.info("Calendar scheduler started (poll every %ds)", POLL_INTERVAL)
    while True:
        try:
            _poll_once()
        except Exception:
            logger.exception("Calendar scheduler error")
        time.sleep(POLL_INTERVAL)


def start(auto_commit_fn=None) -> None:
    """Start the calendar scheduler background thread."""
    global _auto_commit_fn
    _auto_commit_fn = auto_commit_fn

    t = threading.Thread(target=_loop, daemon=True, name="calendar-scheduler")
    t.start()
    logger.info("Calendar scheduler thread started")
