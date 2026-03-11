import logging
import os
import threading

import git

from src.config import DEBOUNCE_SECONDS, GIT_AUTHOR_EMAIL, GIT_AUTHOR_NAME, VAULT_PATH

logger = logging.getLogger(__name__)

LOCK_FILE = os.path.join(VAULT_PATH, ".git", "davyjones.lock")


class DebouncedCommitter:
    """Debounces file system events and auto-commits after a quiet period."""

    def __init__(self):
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.repo = git.Repo(VAULT_PATH)

    def signal_change(self) -> None:
        """Called on every file change. Resets the debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._do_commit)
            self._timer.daemon = True
            self._timer.start()

    def _is_locked(self) -> bool:
        """Check if the dispatcher's status_updater has the lock."""
        return os.path.isfile(LOCK_FILE)

    def _do_commit(self) -> None:
        """Perform the actual git add + commit."""
        # Wait if dispatcher is writing
        if self._is_locked():
            logger.info("Vault locked by dispatcher, deferring commit...")
            # Retry after a short delay
            with self._lock:
                self._timer = threading.Timer(2.0, self._do_commit)
                self._timer.daemon = True
                self._timer.start()
            return

        try:
            # Stage all changes
            self.repo.git.add("-A")

            # Check if there's anything to commit
            if not self.repo.is_dirty(index=True):
                logger.debug("Nothing to commit")
                return

            # Get list of changed files for the commit message
            diff = self.repo.index.diff("HEAD")
            untracked = self.repo.untracked_files
            changed = [d.b_path or d.a_path for d in diff] + untracked

            if not changed:
                # Check for deletions
                changed = ["(file changes)"]

            summary = ", ".join(changed[:5])
            if len(changed) > 5:
                summary += f" (+{len(changed) - 5} more)"

            self.repo.index.commit(
                f"auto: {summary}",
                author=git.Actor(GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL),
                committer=git.Actor(GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL),
            )
            logger.info("Auto-committed %d file(s): %s", len(changed), summary)

        except Exception:
            logger.exception("Auto-commit failed")
