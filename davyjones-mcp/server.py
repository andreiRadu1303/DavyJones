"""DavyJones MCP server — calendar tools for agent use.

Exposes tools to create, list, get, and delete calendar events and
scheduled agent tasks. Reads/writes .davyjones-calendar.json in the
mounted vault directory.
"""

import json
import os
import uuid
from datetime import datetime

from mcp.server.fastmcp import FastMCP

app = FastMCP("davyjones", host="0.0.0.0", port=3004)

VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
CALENDAR_FILE = os.path.join(VAULT_PATH, ".davyjones-calendar.json")


# ── Helpers ──────────────────────────────────────────────────────────


def _load_calendar() -> dict:
    """Load the calendar file, returning a default structure if missing/malformed."""
    try:
        with open(CALENDAR_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "events" not in data:
            raise ValueError("malformed")
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {"version": 1, "calendars": [{"id": "default", "name": "Default", "color": "#7c3aed", "source": "local"}], "events": []}


def _save_calendar(data: dict) -> None:
    """Write calendar data back to disk."""
    with open(CALENDAR_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:8]}"


def _fmt_event(evt: dict) -> str:
    """Format an event for human-readable output."""
    etype = evt.get("type", "event")
    start = evt.get("start", "?")
    end = evt.get("end", "")
    title = evt.get("title", "Untitled")
    parts = [f"[{evt.get('id', '?')}] {title}"]
    if evt.get("allDay"):
        parts.append(f"  all-day: {start}")
    elif end and end != start:
        parts.append(f"  {start} -> {end}")
    else:
        parts.append(f"  {start}")
    if etype == "task":
        parts.append("  type: scheduled task")
        task = evt.get("task", {})
        if task:
            parts.append(f"  agent: {task.get('agentDescription', '')[:100]}")
    if evt.get("recurrence"):
        rec = evt["recurrence"]
        freq = rec.get("freq", "?")
        interval = rec.get("interval", 1)
        parts.append(f"  recurs: every {interval} {freq}")
    if evt.get("description"):
        parts.append(f"  desc: {evt['description'][:150]}")
    parts.append(f"  calendar: {evt.get('calendarId', 'default')}")
    return "\n".join(parts)


# ── Tools ────────────────────────────────────────────────────────────


@app.tool()
def list_calendar_events(date_from: str = "", date_to: str = "") -> str:
    """List calendar events, optionally filtered by date range.

    Args:
        date_from: Start date filter (ISO format YYYY-MM-DD). Events on or after this date.
        date_to: End date filter (ISO format YYYY-MM-DD). Events on or before this date.
    """
    data = _load_calendar()
    events = data.get("events", [])

    if date_from:
        events = [e for e in events if e.get("start", "") >= date_from]
    if date_to:
        events = [e for e in events if e.get("start", "") <= date_to]

    if not events:
        return "No calendar events found."

    events.sort(key=lambda e: e.get("start", ""))
    lines = [_fmt_event(e) for e in events]
    return f"Found {len(events)} event(s):\n\n" + "\n\n".join(lines)


@app.tool()
def get_calendar_event(event_id: str) -> str:
    """Get a specific calendar event by its ID.

    Args:
        event_id: The event ID (e.g., 'evt-a1b2c3d4').
    """
    data = _load_calendar()
    for evt in data.get("events", []):
        if evt.get("id") == event_id:
            return _fmt_event(evt)
    return f"Event not found: {event_id}"


@app.tool()
def create_calendar_event(
    title: str,
    start: str,
    end: str = "",
    description: str = "",
    all_day: bool = False,
    color: str = "",
    calendar_id: str = "default",
) -> str:
    """Create a regular calendar event.

    Args:
        title: Event title.
        start: Start date/time in ISO format (YYYY-MM-DD for all-day, YYYY-MM-DDTHH:mm for timed).
        end: End date/time in ISO format. Defaults to start if omitted.
        description: Optional event description.
        all_day: Whether this is an all-day event.
        color: Optional hex color (e.g., '#ff0000'). Uses calendar default if omitted.
        calendar_id: Calendar to add to (default: 'default').
    """
    data = _load_calendar()
    event_id = _new_event_id()
    evt = {
        "id": event_id,
        "calendarId": calendar_id,
        "title": title,
        "start": start,
        "end": end or start,
        "allDay": all_day,
        "color": color or None,
        "type": "event",
        "description": description,
        "recurrence": None,
        "task": None,
    }
    data["events"].append(evt)
    _save_calendar(data)
    return f"Created event '{title}' (id: {event_id}) at {start}"


@app.tool()
def create_calendar_task(
    title: str,
    start: str,
    agent_description: str,
    scope_files: str = "",
    max_turns: int = 20,
    recurrence_freq: str = "",
    recurrence_interval: int = 1,
    recurrence_by_day: str = "",
    recurrence_until: str = "",
    recurrence_count: int = 0,
    end: str = "",
    color: str = "",
    calendar_id: str = "default",
) -> str:
    """Create a scheduled agent task as a calendar event.

    The dispatcher will automatically run an agent with the given description
    at the scheduled time.

    Args:
        title: Task title.
        start: Start date/time in ISO format (YYYY-MM-DDTHH:mm).
        agent_description: The prompt/instructions for the agent to execute.
        scope_files: Comma-separated list of vault file paths for context (optional).
        max_turns: Max agent iterations (default 20).
        recurrence_freq: Recurrence frequency: 'daily', 'weekly', 'monthly', 'yearly', or '' for none.
        recurrence_interval: Repeat every N periods (e.g., 2 = every 2 weeks). Default 1.
        recurrence_by_day: For weekly recurrence: comma-separated days like 'MO,WE,FR'.
        recurrence_until: End date for recurrence in ISO format (YYYY-MM-DD), or '' for no end.
        recurrence_count: Max number of occurrences, or 0 for unlimited.
        end: End date/time (defaults to start).
        color: Optional hex color.
        calendar_id: Calendar to add to (default: 'default').
    """
    data = _load_calendar()
    event_id = _new_event_id()

    task_config = {
        "agentDescription": agent_description,
        "maxTurns": max_turns,
    }
    if scope_files:
        task_config["scopeFiles"] = [f.strip() for f in scope_files.split(",") if f.strip()]

    recurrence = None
    if recurrence_freq:
        recurrence = {
            "freq": recurrence_freq,
            "interval": recurrence_interval,
        }
        if recurrence_by_day:
            recurrence["byDay"] = [d.strip() for d in recurrence_by_day.split(",") if d.strip()]
        if recurrence_until:
            recurrence["until"] = recurrence_until
        else:
            recurrence["until"] = None
        if recurrence_count > 0:
            recurrence["count"] = recurrence_count
        else:
            recurrence["count"] = None

    evt = {
        "id": event_id,
        "calendarId": calendar_id,
        "title": title,
        "start": start,
        "end": end or start,
        "allDay": False,
        "color": color or None,
        "type": "task",
        "description": "",
        "recurrence": recurrence,
        "task": task_config,
    }
    data["events"].append(evt)
    _save_calendar(data)

    result = f"Created task '{title}' (id: {event_id}) at {start}"
    if recurrence:
        result += f", recurring every {recurrence_interval} {recurrence_freq}"
    return result


@app.tool()
def delete_calendar_event(event_id: str) -> str:
    """Delete a calendar event by its ID.

    Args:
        event_id: The event ID to delete (e.g., 'evt-a1b2c3d4').
    """
    data = _load_calendar()
    events = data.get("events", [])
    original_count = len(events)
    data["events"] = [e for e in events if e.get("id") != event_id]

    if len(data["events"]) == original_count:
        return f"Event not found: {event_id}"

    _save_calendar(data)
    return f"Deleted event {event_id}"


if __name__ == "__main__":
    app.run(transport="sse")
