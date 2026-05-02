"""Tracing models for HungerLoop v0.5a.

:class:`LoopTrace` records the outcome of one loop iteration.

``candidate_state_id`` / ``validation_report_id`` are nullable so the
Orchestrator can emit a trace on empty-plan, SafetyStopError, and worker-
timeout paths where no candidate or validation exists (reverse-spec U10).
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
    candidate_state_id: str | None = None
    validation_report_id: str | None = None
    committed: bool
    delta_summary: str = ""
    blocked_item_ids: list[str] = Field(default_factory=list)
    next_action: str = "continue"


class StopReport(BaseModel):
    """Final stop report for a task."""

    task_id: str
    stop_reason: StopReason
    summary: str = ""
