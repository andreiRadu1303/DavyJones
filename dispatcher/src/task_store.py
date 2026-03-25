"""Task state storage — abstracts in-memory vs Redis-backed task tracking.

In local Docker mode: uses in-memory dicts (same as before).
In cloud K8s mode: uses Redis for persistence across pod restarts.

Selected by RUNTIME_BACKEND env var.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from src.config import RUNTIME_BACKEND

logger = logging.getLogger(__name__)


@dataclass
class TaskState:
    """Serializable task state stored per task."""
    task_id: str
    vault_id: str = ""
    status: str = "queued"  # queued, planning, executing, reporting, done, failed
    phase: str = ""
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    overseer_output: str = ""
    subtasks: list[dict] = field(default_factory=list)
    execution_log: str = ""
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, data: str) -> TaskState:
        d = json.loads(data)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class TaskStore(ABC):
    """Abstract task storage."""

    @abstractmethod
    def put(self, task: TaskState) -> None:
        """Store or update a task."""
        ...

    @abstractmethod
    def get(self, task_id: str) -> Optional[TaskState]:
        """Get a task by ID."""
        ...

    @abstractmethod
    def list_active(self) -> list[TaskState]:
        """List all non-done tasks."""
        ...

    @abstractmethod
    def delete(self, task_id: str) -> None:
        """Remove a task."""
        ...


class MemoryTaskStore(TaskStore):
    """In-memory task store (local Docker mode)."""

    def __init__(self, max_tasks: int = 100):
        self._tasks: dict[str, TaskState] = {}
        self._lock = threading.Lock()
        self._max = max_tasks

    def put(self, task: TaskState) -> None:
        task.updated_at = time.time()
        with self._lock:
            self._tasks[task.task_id] = task
            self._evict()

    def get(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_active(self) -> list[TaskState]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status not in ("done", "failed")]

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def _evict(self):
        """Remove oldest completed tasks when over capacity."""
        if len(self._tasks) <= self._max:
            return
        completed = sorted(
            [(k, v) for k, v in self._tasks.items() if v.status in ("done", "failed")],
            key=lambda x: x[1].updated_at,
        )
        while len(self._tasks) > self._max and completed:
            k, _ = completed.pop(0)
            del self._tasks[k]


class RedisTaskStore(TaskStore):
    """Redis-backed task store (cloud K8s mode)."""

    def __init__(self, redis_url: str = "", prefix: str = "task:"):
        import redis as redis_lib
        url = redis_url or "redis://localhost:6379/0"
        self._client = redis_lib.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._ttl = 86400 * 7  # 7 day TTL for tasks

    def put(self, task: TaskState) -> None:
        task.updated_at = time.time()
        key = f"{self._prefix}{task.task_id}"
        self._client.setex(key, self._ttl, task.to_json())
        # Track active tasks in a set
        if task.status not in ("done", "failed"):
            self._client.sadd(f"{self._prefix}active", task.task_id)
        else:
            self._client.srem(f"{self._prefix}active", task.task_id)

    def get(self, task_id: str) -> Optional[TaskState]:
        key = f"{self._prefix}{task_id}"
        data = self._client.get(key)
        if data:
            return TaskState.from_json(data)
        return None

    def list_active(self) -> list[TaskState]:
        task_ids = self._client.smembers(f"{self._prefix}active")
        tasks = []
        for tid in task_ids:
            t = self.get(tid)
            if t:
                tasks.append(t)
        return tasks

    def delete(self, task_id: str) -> None:
        self._client.delete(f"{self._prefix}{task_id}")
        self._client.srem(f"{self._prefix}active", task_id)


_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    """Get or create the active task store."""
    global _store
    if _store is not None:
        return _store

    if RUNTIME_BACKEND == "k8s":
        import os
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _store = RedisTaskStore(redis_url=redis_url)
        logger.info("Using Redis task store")
    else:
        _store = MemoryTaskStore()
        logger.info("Using in-memory task store")

    return _store
