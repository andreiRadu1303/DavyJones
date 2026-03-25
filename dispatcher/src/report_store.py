"""Report storage — abstracts file-based vs database-backed report persistence.

In local Docker mode: JSON files in REPORTS_DIR (same as before).
In cloud K8s mode: stores in Redis with configurable TTL per tier.

Selected by RUNTIME_BACKEND env var.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Optional

from src.config import MAX_REPORTS, REPORTS_DIR, RUNTIME_BACKEND

logger = logging.getLogger(__name__)


class ReportStore(ABC):
    """Abstract report storage."""

    @abstractmethod
    def save(self, report_id: str, data: dict) -> None:
        """Save a report."""
        ...

    @abstractmethod
    def get(self, report_id: str) -> Optional[dict]:
        """Get a report by ID."""
        ...

    @abstractmethod
    def list_reports(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """List reports, newest first."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Total number of reports."""
        ...


class FileReportStore(ReportStore):
    """JSON file-based report store (local Docker mode)."""

    def __init__(self):
        os.makedirs(REPORTS_DIR, exist_ok=True)
        self._index_path = os.path.join(REPORTS_DIR, "_index.json")

    def _load_index(self) -> list[dict]:
        try:
            with open(self._index_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_index(self, index: list[dict]) -> None:
        with open(self._index_path, "w") as f:
            json.dump(index, f, indent=2)

    def save(self, report_id: str, data: dict) -> None:
        # Save full report
        report_path = os.path.join(REPORTS_DIR, f"{report_id}.json")
        with open(report_path, "w") as f:
            json.dump(data, f, indent=2)

        # Update index (prepend)
        index = self._load_index()
        summary = {
            "id": report_id,
            "created_at": data.get("created_at", time.time()),
            "source": data.get("source", "unknown"),
            "status": data.get("status", "unknown"),
            "summary": data.get("summary", ""),
            "task_count": data.get("task_count", 0),
        }
        index.insert(0, summary)

        # Trim to max
        if len(index) > MAX_REPORTS:
            for old in index[MAX_REPORTS:]:
                old_path = os.path.join(REPORTS_DIR, f"{old['id']}.json")
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            index = index[:MAX_REPORTS]

        self._save_index(index)

    def get(self, report_id: str) -> Optional[dict]:
        report_path = os.path.join(REPORTS_DIR, f"{report_id}.json")
        try:
            with open(report_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def list_reports(self, limit: int = 20, offset: int = 0) -> list[dict]:
        index = self._load_index()
        return index[offset:offset + limit]

    def count(self) -> int:
        return len(self._load_index())


class RedisReportStore(ReportStore):
    """Redis-backed report store (cloud K8s mode)."""

    def __init__(self, redis_url: str = "", prefix: str = "report:"):
        import redis as redis_lib
        url = redis_url or "redis://localhost:6379/0"
        self._client = redis_lib.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._index_key = f"{prefix}index"
        self._ttl = 86400 * 90  # 90 days default (overridden per tier)

    def save(self, report_id: str, data: dict) -> None:
        key = f"{self._prefix}{report_id}"
        self._client.setex(key, self._ttl, json.dumps(data, default=str))

        # Add to sorted set (score = timestamp for ordering)
        ts = data.get("created_at", time.time())
        summary = json.dumps({
            "id": report_id,
            "created_at": ts,
            "source": data.get("source", "unknown"),
            "status": data.get("status", "unknown"),
            "summary": data.get("summary", ""),
            "task_count": data.get("task_count", 0),
        })
        self._client.zadd(self._index_key, {summary: ts})

        # Trim to max
        excess = self._client.zcard(self._index_key) - MAX_REPORTS
        if excess > 0:
            # Remove oldest entries
            old_entries = self._client.zrange(self._index_key, 0, excess - 1)
            for entry in old_entries:
                try:
                    old_data = json.loads(entry)
                    self._client.delete(f"{self._prefix}{old_data['id']}")
                except Exception:
                    pass
            self._client.zremrangebyrank(self._index_key, 0, excess - 1)

    def get(self, report_id: str) -> Optional[dict]:
        key = f"{self._prefix}{report_id}"
        data = self._client.get(key)
        if data:
            return json.loads(data)
        return None

    def list_reports(self, limit: int = 20, offset: int = 0) -> list[dict]:
        # Reverse order (newest first)
        entries = self._client.zrevrange(self._index_key, offset, offset + limit - 1)
        results = []
        for entry in entries:
            try:
                results.append(json.loads(entry))
            except json.JSONDecodeError:
                pass
        return results

    def count(self) -> int:
        return self._client.zcard(self._index_key)


_store: ReportStore | None = None


def get_report_store() -> ReportStore:
    """Get or create the active report store."""
    global _store
    if _store is not None:
        return _store

    if RUNTIME_BACKEND == "k8s":
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _store = RedisReportStore(redis_url=redis_url)
        logger.info("Using Redis report store")
    else:
        _store = FileReportStore()
        logger.info("Using file-based report store")

    return _store
