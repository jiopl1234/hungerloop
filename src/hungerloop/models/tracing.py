"""Tracing models for HungerLoop v0.5a.

:class:`LoopTrace` records the outcome of one loop iteration. v0.5.2
extends it (§13.1) with check-progress, per-loop usage deltas, and an
optional ``stop_reason`` so the same shape works for normal-completion
and stop paths.

:class:`StopReport` is the final per-task output. v0.5.2 (§13.2 + §28.6 /
M20) gives it a typed :data:`GoalStatus`, totals (cost / tokens / loops),
and remaining-item bookkeeping. The mapping
``StopReason → GoalStatus`` lives in
:mod:`hungerloop.services.stop_report_builder` so :class:`StopReport`
itself stays a pure data container.

``candidate_state_id`` / ``validation_report_id`` on :class:`LoopTrace`
remain nullable (reverse-spec U10) so empty-plan, SafetyStop, and
worker-timeout paths emit a trace without forcing a candidate.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from hungerloop.models.enums import StopReason

GoalStatus = Literal["completed", "partial", "blocked", "abandoned", "paused"]
"""Final outcome label for a task (PRD §28.6 / M20).

The five values mirror UX expectations: ``completed`` and ``partial`` are
the only "successful" outcomes from a goal-tracking perspective;
``blocked`` and ``abandoned`` are negative; ``paused`` is suspended-but-
recoverable. The mapping from :class:`StopReason` is enforced by
:func:`hungerloop.services.stop_report_builder.build_stop_report`.
"""


class LoopTrace(BaseModel):
    """Trace of one loop iteration (PRD §13.1)."""

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

    # Check-level progress signals; empty when the loop produced no candidate.
    newly_passed_check_keys: list[str] = Field(default_factory=list)
    regressed_check_keys: list[str] = Field(default_factory=list)
    blocked_items_added: list[str] = Field(default_factory=list)

    # Per-loop usage deltas (computed by Orchestrator from UsageSnapshot
    # before/after; tokens include both prompt and completion).
    tokens_consumed_this_loop: int = 0
    cost_this_loop_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0

    delta_summary: str = ""
    blocked_item_ids: list[str] = Field(default_factory=list)
    next_action: str = "continue"
    stop_reason: StopReason | None = None


class StopReport(BaseModel):
    """Final stop report for a task (PRD §13.2 + §28.6 / M20)."""

    task_id: str
    stop_reason: StopReason
    goal_status: GoalStatus

    final_best_state_id: str | None = None
    best_state_summary: str | None = None
    accepted_check_keys_count: int = 0

    total_loops: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0

    remaining_hunger_items: list[str] = Field(default_factory=list)
    blocked_hunger_items: list[str] = Field(default_factory=list)

    recommended_refill: float | None = None
    recommendation: str = ""
    summary: str = ""
