"""Hunger domain models for HungerLoop v0.4.1.

Contains the baseline v0.4 models (:class:`AcceptanceCheck`, :class:`HungerItem`,
:class:`HungerPolicy`, :class:`HungerClockState`, :class:`HungerSnapshot`) and the
v0.4.1 :class:`HungerLedger` that encodes invariant I-9 (BLOCKED items do not
count as DONE).

Status model:
    - OPEN / WORKING — actionable by the agent; contribute to work pressure.
    - BLOCKED        — the agent is stuck; excluded from ``active_items`` but
                       still counted as unfinished so ``is_done()`` stays False.
                       The orchestrator emits ``StopReason.BLOCKED`` when every
                       remaining item is BLOCKED.
    - PAUSED         — held for human input; also excluded from ``active_items``
                       and also counted as unfinished (the orchestrator emits
                       ``StopReason.HUMAN_PAUSED``). PAUSED is intentionally
                       *not* merged with BLOCKED — they map to different stop
                       reasons and drive different recovery flows.
    - CLOSED / VALIDATED_SATISFIED — terminal-done; excluded from
                       ``unfinished_items``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from hungerloop.models.enums import (
    AcceptanceCheckType,
    CompletionMode,
    DecayType,
    HungerItemStatus,
    HungerItemType,
    LoopPhase,
    StopReason,
)


class AcceptanceCheck(BaseModel):
    check_type: AcceptanceCheckType
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class HungerItem(BaseModel):
    id: str
    title: str
    item_type: HungerItemType = HungerItemType.GOAL_GAP
    priority: float = 1.0
    gap_score: float = 1.0
    status: HungerItemStatus = HungerItemStatus.OPEN

    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)
    acceptance_mode: Literal["all", "any"] = "all"

    consecutive_failure_count: int = 0
    last_progress_loop_id: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    updated_at_loop: int = 0
    refinement_tier: int = 0
    refinement_kind: str = "correctness"
    generated_by: str | None = None
    source_check_keys: list[str] = Field(default_factory=list)

    # v0.7 synthesized-check lifecycle state. These fields are meaningful only
    # for compiler-owned H-SYN items and persist inside existing ledger JSON.
    synthesis_baseline_pending: bool = False
    synthesis_fixture_argv: list[str] | None = None
    synthesis_prerequisite_check_keys: list[str] = Field(default_factory=list)
    synthesis_conflict_signatures: dict[str, int] = Field(default_factory=dict)
    synthesis_resolution_kind: str | None = None
    synthesis_resolution_reason: str | None = None


_INACTIVE_STATUSES: frozenset[HungerItemStatus] = frozenset(
    {
        HungerItemStatus.CLOSED,
        HungerItemStatus.PAUSED,
        HungerItemStatus.BLOCKED,
    }
)
"""Statuses that exclude an item from ``active_items`` (cannot be progressed)."""

_DONE_STATUSES: frozenset[HungerItemStatus] = frozenset(
    {
        HungerItemStatus.CLOSED,
        HungerItemStatus.VALIDATED_SATISFIED,
    }
)
"""Statuses that mark an item as terminal-finished (excluded from ``unfinished_items``)."""


class HungerLedger(BaseModel):
    task_id: str
    items: list[HungerItem] = Field(default_factory=list)

    def active_items(self) -> list[HungerItem]:
        return [
            item
            for item in self.items
            if item.status not in _INACTIVE_STATUSES
            and item.gap_score > 0
        ]

    def blocked_items(self) -> list[HungerItem]:
        return [
            item
            for item in self.items
            if item.status == HungerItemStatus.BLOCKED and item.gap_score > 0
        ]

    def unfinished_items(self) -> list[HungerItem]:
        return [
            item
            for item in self.items
            if item.status not in _DONE_STATUSES
            and item.gap_score > 0
        ]

    def unfinished_items_by_tier(self, max_tier: int) -> list[HungerItem]:
        return [
            item
            for item in self.unfinished_items()
            if item.refinement_tier <= max_tier
        ]

    def tier_is_done(self, tier: int) -> bool:
        tier_items = [
            item for item in self.items if item.refinement_tier == tier
        ]
        return bool(tier_items) and all(
            item.status in _DONE_STATUSES or item.gap_score <= 0
            for item in tier_items
        )

    def all_tiers_done(self, max_tier: int) -> bool:
        return all(self.tier_is_done(tier) for tier in range(max_tier + 1))

    def work_pressure(self) -> float:
        return sum(item.priority * item.gap_score for item in self.active_items())

    def has_active_items(self) -> bool:
        return bool(self.active_items())

    def has_blocked_items(self) -> bool:
        return bool(self.blocked_items())

    def all_remaining_items_blocked(self) -> bool:
        unfinished = self.unfinished_items()
        return bool(unfinished) and all(
            item.status == HungerItemStatus.BLOCKED for item in unfinished
        )

    def is_done(self) -> bool:
        return not self.unfinished_items()


class HungerPolicy(BaseModel):
    initial_hunger: float = 100.0
    h_max: float = 100.0
    decay_type: DecayType = DecayType.LOOP_COUNT
    decay_duration_seconds: float = 10.0
    started_at: datetime | None = None
    max_total_cost_usd: float = 10.0
    max_total_tokens: int = 1_000_000
    completion_mode: CompletionMode = CompletionMode.STOP_ON_DONE
    refinement_profile: str | None = None
    max_refinement_tier: int = 0
    respect_stagnation: bool = True

    # ---- v0.7 feature flags (defaults preserve v0.6 behavior) ----
    synthesis_enabled: bool = False
    synthesis_plan_time_tier: int = 0
    synthesis_max_total_items: int = 20
    synthesis_max_active_items: int = 3
    synthesis_batch_size: int = 3
    synthesis_conflict_threshold: int = 2
    synthesis_audit_enabled: bool = False
    regression_confirm_reruns: int = 2
    rejected_candidate_continuation_enabled: bool = True
    rejected_candidate_continuation_max_chain: int = 2
    refactor_transactions_enabled: bool = False
    max_declared_regressions: int = 5
    refactor_deadline_loops: int = 3
    # ---- v0.7.x: autonomous ADR-010 trigger. Off by default; also requires
    # refactor_transactions_enabled. The trigger only *opens* a transaction —
    # tolerance, deadline, and rollback stay in RefactorTransactionManager /
    # CommitManager (the sanctioned I-3 amendment, not a new one).
    refactor_auto_open_enabled: bool = False
    refactor_auto_open_min_newly: int = 5
    # Global no-progress fuse threshold (loops without a committed candidate
    # before StopReason.BLOCKED). Momentum holds (strictly growing rejected
    # newly-passed counts) do not consume this budget.
    max_global_no_progress_loops: int = 5
    memory_auto_promote_enabled: bool = True
    memory_recall_enabled: bool = True
    # ---- v0.7.2: cold-start draft sampling (default preserves v0.7.1) ----
    # AIDE-style drafting: on a task's FIRST loop only, sample k independent
    # candidate drafts and promote the score-free winner. 1 disables.
    draft_sampling_k: int = 1

    @field_validator("synthesis_plan_time_tier")
    @classmethod
    def _validate_synthesis_plan_time_tier(cls, v: int) -> int:
        if v < 0:
            raise ValueError("synthesis_plan_time_tier must not be negative")
        return v

    @field_validator("synthesis_max_total_items")
    @classmethod
    def _validate_synthesis_max_total_items(cls, v: int) -> int:
        if v < 0:
            raise ValueError("synthesis_max_total_items must not be negative")
        return v

    @field_validator("draft_sampling_k")
    @classmethod
    def _validate_draft_sampling_k(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError("draft_sampling_k must be between 1 and 5")
        return v

    @field_validator(
        "synthesis_max_active_items",
        "synthesis_batch_size",
        "synthesis_conflict_threshold",
    )
    @classmethod
    def _validate_positive_synthesis_limits(cls, v: int) -> int:
        if v < 1:
            raise ValueError("synthesis lifecycle limits must be >= 1")
        return v

    @field_validator("regression_confirm_reruns")
    @classmethod
    def _validate_regression_confirm_reruns(cls, v: int) -> int:
        if v < 0:
            raise ValueError("regression_confirm_reruns must not be negative")
        return v

    @field_validator("rejected_candidate_continuation_max_chain")
    @classmethod
    def _validate_rejected_candidate_continuation_max_chain(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                "rejected_candidate_continuation_max_chain must not be negative"
            )
        return v

    @field_validator("max_declared_regressions")
    @classmethod
    def _validate_max_declared_regressions(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_declared_regressions must not be negative")
        return v

    @field_validator("refactor_deadline_loops")
    @classmethod
    def _validate_refactor_deadline_loops(cls, v: int) -> int:
        if v < 0:
            raise ValueError("refactor_deadline_loops must not be negative")
        return v

    @field_validator("refactor_auto_open_min_newly")
    @classmethod
    def _validate_refactor_auto_open_min_newly(cls, v: int) -> int:
        if v < 1:
            raise ValueError("refactor_auto_open_min_newly must be positive")
        return v

    @field_validator("max_global_no_progress_loops")
    @classmethod
    def _validate_max_global_no_progress_loops(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_global_no_progress_loops must be positive")
        return v


class HungerClockState(BaseModel):
    loop_count: int = 0
    frozen: bool = False
    consumed_by_cost_usd: float = 0.0
    consumed_tokens: int = 0
    manually_cleared: bool = False


class HungerSnapshot(BaseModel):
    drive_budget: float
    work_pressure: float
    active_hunger: float
    drive_ratio: float
    phase: LoopPhase
    should_stop: bool
    stop_reason: StopReason | None = None
