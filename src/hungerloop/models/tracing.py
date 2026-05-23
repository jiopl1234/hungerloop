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

from typing import Any, Literal

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
    """Trace of one loop iteration (PRD §13.1).

    v0.5d.0 (PRD §6.2 / §8.2) extends this with the additive list
    fields the orchestrator's event-aware path needs:

    * ``best_state_id_before_loop`` / ``_after_loop`` — anchor pair
      so a downstream auditor can replay best/ promotions without
      re-querying the candidate row.
    * ``verdict`` — short ValidationReport.verdict snapshot.
    * ``currently_passed_check_keys`` — full set of checks passing
      AFTER this loop (vs ``newly_passed`` which is the delta).
    * ``satisfied_hunger_item_ids`` / ``unsatisfied_hunger_item_ids``
      — aggregated per-loop snapshot from the validation step.
    * ``model_errors`` / ``tool_errors`` / ``worker_errors`` —
      string descriptions of errors observed during this loop, used
      by the synthetic-worker-exception persistence path so an
      ERROR loop still emits a trace row.

    All new fields default to their empty value so c0-01-shipped /
    v0.5b-shipped rows deserialise cleanly.
    """

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

    # v0.5d.0 additions — full passing-check snapshot at end of loop.
    currently_passed_check_keys: list[str] = Field(default_factory=list)
    satisfied_hunger_item_ids: list[str] = Field(default_factory=list)
    unsatisfied_hunger_item_ids: list[str] = Field(default_factory=list)

    # v0.5d.0 additions — best_state anchors and verdict snapshot.
    best_state_id_before_loop: str | None = None
    best_state_id_after_loop: str | None = None
    verdict: str | None = None

    # v0.5d.0 additions — error log for synthetic-exception persistence.
    model_errors: list[str] = Field(default_factory=list)
    tool_errors: list[str] = Field(default_factory=list)
    worker_errors: list[str] = Field(default_factory=list)

    # v0.6 additions — mission runtime trace snapshots. Legacy non-mission
    # loops keep these empty so persisted v0.5f traces deserialize unchanged.
    mission_snapshot: dict[str, Any] | None = None
    assignment_traces: list[dict[str, Any]] = Field(default_factory=list)
    validation_pipeline_trace: dict[str, Any] | None = None

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


# Per-stop-reason resume hints rendered into ``StopReport.resume_hint``
# when the writer hasn't already supplied one (PRD §9.3). Keys are
# ``StopReason.value`` strings — NOT enum names — so callers can look
# them up as ``STOP_REASON_HINTS[report.stop_reason.value]``.
STOP_REASON_HINTS: dict[str, str] = {
    "done": "Task is complete. Use --reset to start a new run.",
    "hunger_expired": "Refill hunger before resuming.",
    "budget_exhausted": (
        "Loop budget was intentionally exhausted. Refill loops or reset to continue."
    ),
    "blocked": "Unblock hunger items before resuming.",
    "human_required": (
        "Resolve the requested human action, then run with --resume."
    ),
    "human_paused": "Resume hunger or pass --resume.",
    "safety_stop": "Raise cost/token ceiling before resuming.",
    "error": (
        "Inspect trace/report, repair state, then resume or reset."
    ),
}


class StopReport(BaseModel):
    """Final stop report for a task (PRD §13.2 + §28.6 / M20).

    v0.5d.0 additions (PRD §9.2):

    * ``accepted_check_keys`` — full list of accepted keys at stop
      time (the c0-01-shipped ``accepted_check_keys_count`` is now
      the int-summary form of this list).
    * ``llm_calls`` / ``tool_calls`` — totals so consumers don't
      have to re-aggregate from UsageSnapshot.
    * ``last_validation_report_id`` / ``last_loop_id`` — pointers
      for ``hungerloop trace`` to jump to the last loop's data.
    * ``recommended_next_actions`` — list-form remediation; the
      shipped ``recommendation`` string remains for backwards
      compat.
    * ``resume_hint`` — auto-populated from STOP_REASON_HINTS
      keyed by ``stop_reason.value`` when not explicitly set by
      the writer.
    """

    task_id: str
    stop_reason: StopReason
    goal_status: GoalStatus

    final_best_state_id: str | None = None
    best_state_summary: str | None = None

    accepted_check_keys_count: int = 0
    accepted_check_keys: list[str] = Field(default_factory=list)

    total_loops: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0

    remaining_hunger_items: list[str] = Field(default_factory=list)
    blocked_hunger_items: list[str] = Field(default_factory=list)

    last_validation_report_id: str | None = None
    last_loop_id: int | None = None

    recommended_refill: float | None = None
    recommended_next_actions: list[str] = Field(default_factory=list)
    resume_hint: str | None = None

    recommendation: str = ""
    summary: str = ""
