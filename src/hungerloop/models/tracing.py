"""Tracing models for HungerLoop v0.4.1.

:class:`LoopTrace` records the outcome of one loop iteration.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from hungerloop.models.enums import StopReason


class LoopTrace(BaseModel):
    """Trace of one loop iteration."""

    task_id: str
    loop_id: int
    phase: str
    active_hunger: float
    drive_budget: float
    work_pressure: float
    selected_hunger_item_ids: list[str] = Field(default_factory=list)
    worker_ids: list[str] = Field(default_factory=list)
    candidate_state_id: str
    validation_report_id: str
    committed: bool
    delta_summary: str = ""
    blocked_item_ids: list[str] = Field(default_factory=list)
    next_action: str = "continue"


class StopReport(BaseModel):
    """Final stop report for a task."""

    task_id: str
    stop_reason: StopReason
    summary: str = ""
