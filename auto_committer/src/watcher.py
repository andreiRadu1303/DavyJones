import logging
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

IGNORE_DIRS = {".obsidian", ".git"}
IGNORE_SUFFIXES = (".swp", ".tmp", "~")


class VaultEventHandler(FileSystemEventHandler):
    """Watches for .md file changes, calls on_change callback with debouncing handled externally."""

    def __init__(self, on_change: Callable[[], None]):
        super().__init__()
        self.on_change = on_change

    def _should_ignore(self, path: str) -> bool:
        parts = path.split("/")
        if any(p in IGNORE_DIRS for p in parts):
            return True
        if any(path.endswith(s) for s in IGNORE_SUFFIXES):
            return True
        return False

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return
        path = event.src_path
        if self._should_ignore(path):
            return
        logger.debug("File event: %s %s", event.event_type, path)
        self.on_change()


def start_observer(vault_path: str, on_change: Callable[[], None]) -> Observer:
    """Start watching the vault directory for file changes."""
    handler = VaultEventHandler(on_change)
    observer = Observer()
    observer.schedule(handler, vault_path, recursive=True)
    observer.start()
    logger.info("Watching %s for file changes", vault_path)
    return observer
