from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from hungerloop.models.enums import (
    AcceptanceCheckType,
    DecayType,
    HungerItemStatus,
    HungerItemType,
    LoopPhase,
    StopReason,
)


class AcceptanceCheck(BaseModel):
    check_type: AcceptanceCheckType
    params: dict[str, object] = Field(default_factory=dict)
    description: str = ""


class HungerItem(BaseModel):
    id: str
    title: str
    item_type: HungerItemType = HungerItemType.GOAL_GAP
    priority: float = 1.0
    gap_score: float = 1.0
    status: HungerItemStatus = HungerItemStatus.OPEN

    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)
    acceptance_mode: str = "all"

    consecutive_failure_count: int = 0
    last_progress_loop_id: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    updated_at_loop: int = 0


class HungerLedger(BaseModel):
    task_id: str
    items: list[HungerItem] = Field(default_factory=list)

    def active_items(self) -> list[HungerItem]:
        return [
            item
            for item in self.items
            if item.status
            not in {
                HungerItemStatus.CLOSED,
                HungerItemStatus.PAUSED,
                HungerItemStatus.BLOCKED,
            }
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
            if item.status
            not in {
                HungerItemStatus.CLOSED,
                HungerItemStatus.VALIDATED_SATISFIED,
            }
            and item.gap_score > 0
        ]

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
