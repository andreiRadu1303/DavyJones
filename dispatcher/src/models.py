from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class NoteMetadata(BaseModel):
    type: str = "note"
    status: Optional[str] = None
    priority: str = "normal"
    depends_on: list[str] = []
    tags: list[str] = []
    max_iterations: Optional[int] = None
    model_config = {"extra": "allow"}


class DispatchPayload(BaseModel):
    task_file_path: str
    prompt: str
    context: str
    metadata: dict[str, Any]


class TaskResult(BaseModel):
    status: str  # "completed" or "failed"
    output_text: Optional[str] = None
    files_modified: list[str] = []
    error: Optional[str] = None
    hit_max_turns: bool = False
