"""Data models for tasks and sessions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    user_id: str = ""
    session_id: str = ""
    message: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    output: str = ""
    summary: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str = ""
    finished_at: str = ""


@dataclass
class Session:
    user_id: str
    session_id: str = field(default_factory=lambda: str(uuid4()))
    working_dir: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_used_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    initialized: bool = False
