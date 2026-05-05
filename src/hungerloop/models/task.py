"""Task metadata models for persisted HungerLoop runs."""
from __future__ import annotations

from pydantic import BaseModel

from hungerloop.models.enums import StopReason


class TaskRecord(BaseModel):
    """Minimal persisted task row used by CLI status/report/trace surfaces."""

    task_id: str
    raw_goal: str
    status: str = "pending"
    last_stop_reason: StopReason | None = None
    created_at: str = ""
    updated_at: str = ""
