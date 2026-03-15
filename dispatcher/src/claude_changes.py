"""Track files changed by Claude auto-commits."""
import threading

_claude_changed_files: set[str] = set()
_lock = threading.Lock()


def record_changed_files(files: list[str]) -> None:
    """Add files to the changed set."""
    with _lock:
        _claude_changed_files.update(files)


def get_changed_files() -> list[str]:
    """Return sorted list of files changed by Claude since last clear."""
    with _lock:
        return sorted(_claude_changed_files)


def clear_changed_files() -> None:
    """Clear the set of Claude-changed files."""
    with _lock:
        _claude_changed_files.clear()
