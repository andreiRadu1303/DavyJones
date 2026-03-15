import logging
import os
import threading
from datetime import datetime, timezone

import frontmatter

from src.config import VAULT_PATH
from src.models import TaskResult

logger = logging.getLogger(__name__)

LOCK_FILE = os.path.join(VAULT_PATH, ".git", "davyjones.lock")

_lock = threading.Lock()


def _acquire_lock() -> None:
    """Acquire in-process lock and write file signal for auto-committer."""
    _lock.acquire()
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _release_lock() -> None:
    """Remove file signal and release in-process lock."""
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    finally:
        _lock.release()


def update_status(
    file_path: str,
    new_status: str,
    result: TaskResult | None = None,
) -> None:
    """Update the status field in a task file's YAML frontmatter.

    Only modifies files that have `type: task` or `type: job` in their
    frontmatter. Regular vault files are left untouched — we don't want
    to inject status metadata into a user's game notes or other content.

    file_path is relative to VAULT_PATH.
    """
    full_path = os.path.join(VAULT_PATH, file_path)
    if not os.path.isfile(full_path):
        return

    _acquire_lock()
    try:
        post = frontmatter.load(full_path)

        # Only update files that are actually task/job files
        file_type = post.metadata.get("type", "")
        if file_type not in ("task", "job"):
            logger.debug("Skipping status update for %s (type=%r, not a task file)", file_path, file_type)
            return

        post.metadata["status"] = new_status

        if new_status == "completed":
            post.metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        elif new_status == "failed" and result and result.error:
            post.metadata["error_message"] = result.error

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info("Updated status of %s to %s", file_path, new_status)
    finally:
        _release_lock()
