import logging
import os
from datetime import datetime, timezone

import frontmatter

from src.config import VAULT_PATH
from src.models import TaskResult

logger = logging.getLogger(__name__)

LOCK_FILE = os.path.join(VAULT_PATH, ".git", "davyjones.lock")


def _acquire_lock() -> None:
    """Simple file-based lock."""
    lock_dir = os.path.dirname(LOCK_FILE)
    os.makedirs(lock_dir, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def _release_lock() -> None:
    """Release file-based lock."""
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def update_status(
    file_path: str,
    new_status: str,
    result: TaskResult | None = None,
) -> None:
    """Update the status field in a task file's YAML frontmatter.

    file_path is relative to VAULT_PATH.
    """
    full_path = os.path.join(VAULT_PATH, file_path)
    if not os.path.isfile(full_path):
        logger.error("Cannot update status: file not found: %s", file_path)
        return

    _acquire_lock()
    try:
        post = frontmatter.load(full_path)
        post.metadata["status"] = new_status

        if new_status == "completed":
            post.metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        elif new_status == "failed" and result and result.error:
            post.metadata["error_message"] = result.error

        # Append results to content if completed
        if new_status == "completed" and result and result.output_text:
            separator = "\n\n---\n\n## Agent Results\n\n"
            post.content = post.content.rstrip() + separator + result.output_text

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info("Updated status of %s to %s", file_path, new_status)
    finally:
        _release_lock()
