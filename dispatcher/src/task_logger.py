"""task_logger — appends a one-line entry to the vault's DavyJones Tasks note.

Called after every DirectTask completes (whether from the plugin, the calendar,
a commit, or Slack) so there is a permanent human-readable log in the vault.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from src.config import VAULT_PATH

logger = logging.getLogger(__name__)

_LOG_LOCK = threading.Lock()
_NOTE_NAME = "DavyJones Tasks.md"
_HEADER = "# DavyJones Tasks\n\n"


def append_task_log(
    created_at: str,
    description: str,
    scope_files: list[str],
    status: str,
    succeeded: int,
    task_count: int,
    error: str | None,
    source: str = "direct",
) -> None:
    """Append a single log line to the DavyJones Tasks vault note."""
    note_path = os.path.join(VAULT_PATH, _NOTE_NAME)

    try:
        dt = datetime.fromisoformat(created_at)
        time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        time_str = created_at

    if scope_files:
        shown = scope_files[:3]
        scope_str = ", ".join(shown)
        if len(scope_files) > 3:
            scope_str += f" (+{len(scope_files) - 3} more)"
    else:
        scope_str = "auto"

    if status == "completed":
        if task_count == 0:
            outcome = "Completed — no tasks needed"
        else:
            outcome = f"Completed — {succeeded}/{task_count} sub-tasks"
    elif status == "failed":
        err_snippet = (error or "unknown error")[:120]
        if task_count > 0:
            outcome = f"Failed ({succeeded}/{task_count}) — {err_snippet}"
        else:
            outcome = f"Failed — {err_snippet}"
    else:
        outcome = status

    desc_display = description[:200]
    line = f"- `{time_str}` [{source}] {desc_display} | `{scope_str}` | {outcome}\n"

    try:
        with _LOG_LOCK:
            if not os.path.isfile(note_path):
                with open(note_path, "w", encoding="utf-8") as f:
                    f.write(_HEADER)
            with open(note_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        logger.exception("Failed to write task log to %s", note_path)
