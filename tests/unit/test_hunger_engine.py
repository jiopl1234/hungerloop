"""Unit tests for HungerEngine — decay paths AND tick() branch coverage.

`hunger_engine.py` had ~67% coverage before this file landed; LOOP_COUNT
was exercised through test_loop_count_decay but STAGE_BASED was only
covered by enum tests, and the LOOP_COUNT max_loops_for_decay=0 boundary
was untested.

The ``tick()`` branches were also under-tested: the orchestrator integration
suite hits the happy DONE path, but the StopReason priority order (I-9) was
never asserted directly. Tests below pin each branch and the critical
priority cases (frozen > cost > tokens > BLOCKED > HUNGER_EXPIRED > DONE).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hungerloop.models.enums import (
    DecayType,
    HungerItemStatus,
    LoopPhase,
    StopReason,
)
from hungerloop.models.hunger import (
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.services.hunger_engine import HungerEngine


def test_stage_based_decay_at_50_percent_elapsed() -> None:
    engine = HungerEngine()
    now = datetime.now(timezone.utc)
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=100.0,
        started_at=now - timedelta(seconds=50),
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(policy, clock, now)
    assert 40.0 < budget < 60.0, (
        f"STAGE_BASED at 50% elapsed should yield ~50; got {budget}"
    )


def test_stage_based_decay_past_duration_returns_zero() -> None:
    engine = HungerEngine()
    now = datetime.now(timezone.utc)
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=10.0,
        started_at=now - timedelta(seconds=999),
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(policy, clock, now)
    assert budget == 0.0


def test_stage_based_decay_with_no_started_at_returns_initial() -> None:
    """STAGE_BASED needs `started_at`; without it, fall through to
    `initial_hunger` instead of crashing."""
    engine = HungerEngine()
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=100.0,
        started_at=None,
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(
        policy, clock, datetime.now(timezone.utc)
    )
    assert budget == 100.0


def test_loop_count_max_zero_does_not_divide_by_zero() -> None:
    """Boundary: LOOP_COUNT decay with max_loops_for_decay=0 must not
    raise ZeroDivisionError."""
    engine = HungerEngine()
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.LOOP_COUNT,
        max_loops_for_decay=0,
    )
    clock = HungerClockState(task_id="t1")
    try:
        budget = engine._compute_drive_budget(
            policy, clock, datetime.now(timezone.utc)
        )
    except ZeroDivisionError:
        pytest.fail(
            "LOOP_COUNT with max_loops_for_decay=0 raised ZeroDivisionError "
            "instead of returning a sensible value"
        )
    assert isinstance(budget, float)


# ---- tick() branch coverage --------------------------------------------------


def _open_item(item_id: str = "H-001", gap: float = 1.0) -> HungerItem:
    return HungerItem(id=item_id, title="x", gap_score=gap)


def _blocked_item(item_id: str = "H-001", gap: float = 1.0) -> HungerItem:
    return HungerItem(
        id=item_id, title="x", gap_score=gap, status=HungerItemStatus.BLOCKED
    )


def _done_item(item_id: str = "H-001") -> HungerItem:
    # gap_score=0 with VALIDATED_SATISFIED puts it in _DONE_STATUSES so
    # ledger.is_done() returns True.
    return HungerItem(
        id=item_id,
        title="x",
        gap_score=0.0,
        status=HungerItemStatus.VALIDATED_SATISFIED,
    )


def _fresh_policy(initial: float = 100.0, max_cost: float = 10.0) -> HungerPolicy:
    """LOOP_COUNT policy with a generous budget so drive>0 unless we say so."""
    return HungerPolicy(
        initial_hunger=initial,
        decay_type=DecayType.LOOP_COUNT,
        decay_duration_seconds=100.0,  # treated as max_loops here
        max_total_cost_usd=max_cost,
        max_total_tokens=1_000_000,
    )


def test_tick_frozen_clock_emits_human_paused() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState(frozen=True)
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.HUMAN_PAUSED


def test_tick_cost_ceiling_emits_safety_stop() -> None:
    engine = HungerEngine()
    policy = _fresh_policy(max_cost=5.0)
    clock = HungerClockState(consumed_by_cost_usd=5.0)
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.SAFETY_STOP


def test_tick_token_ceiling_emits_safety_stop() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    policy = policy.model_copy(update={"max_total_tokens": 1000})
    clock = HungerClockState(consumed_tokens=1000)
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.SAFETY_STOP


def test_tick_all_blocked_emits_blocked_not_done() -> None:
    """I-9: BLOCKED takes priority over DONE. With all unfinished items
    BLOCKED, ledger.is_done() returns False (BLOCKED items are still
    unfinished), so the BLOCKED branch must win."""
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState()
    ledger = HungerLedger(
        task_id="t1",
        items=[_blocked_item("H-001"), _blocked_item("H-002")],
    )
    assert ledger.all_remaining_items_blocked() is True
    assert ledger.is_done() is False

    snap = engine.tick(policy, clock, ledger)
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.BLOCKED


def test_tick_drive_zero_with_unfinished_emits_hunger_expired() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState(manually_cleared=True)  # forces drive=0
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.drive_budget == 0.0
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.HUNGER_EXPIRED


def test_tick_done_with_drive_remaining_emits_done() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState()
    ledger = HungerLedger(task_id="t1", items=[_done_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.drive_budget > 0
    assert snap.should_stop is True
    assert snap.stop_reason is StopReason.DONE


def test_tick_active_work_with_budget_does_not_stop() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState()
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.should_stop is False
    assert snap.stop_reason is None


# ---- tick() priority order (I-9) ---------------------------------------------


def test_tick_priority_frozen_beats_cost_ceiling() -> None:
    """frozen=True + cost-over: HUMAN_PAUSED wins (humans take precedence)."""
    engine = HungerEngine()
    policy = _fresh_policy(max_cost=5.0)
    clock = HungerClockState(frozen=True, consumed_by_cost_usd=999.0)
    ledger = HungerLedger(task_id="t1", items=[_open_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.stop_reason is StopReason.HUMAN_PAUSED


def test_tick_priority_safety_beats_blocked() -> None:
    """cost over + all blocked: SAFETY_STOP wins (don't recover from blocked
    when we're already over budget)."""
    engine = HungerEngine()
    policy = _fresh_policy(max_cost=5.0)
    clock = HungerClockState(consumed_by_cost_usd=10.0)
    ledger = HungerLedger(task_id="t1", items=[_blocked_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.stop_reason is StopReason.SAFETY_STOP


def test_tick_priority_safety_beats_done() -> None:
    """cost over + ledger done: SAFETY_STOP wins. The orchestrator must
    emit safety, not done, so the operator notices the budget breach."""
    engine = HungerEngine()
    policy = _fresh_policy(max_cost=5.0)
    clock = HungerClockState(consumed_by_cost_usd=10.0)
    ledger = HungerLedger(task_id="t1", items=[_done_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.stop_reason is StopReason.SAFETY_STOP


def test_tick_priority_blocked_beats_hunger_expired() -> None:
    """I-9: all blocked + drive=0: BLOCKED wins. Operator should refill
    AND unblock, not just refill."""
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState(manually_cleared=True)  # drive=0
    ledger = HungerLedger(task_id="t1", items=[_blocked_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.drive_budget == 0.0
    assert snap.stop_reason is StopReason.BLOCKED


def test_tick_priority_done_when_drive_zero_and_ledger_done() -> None:
    """drive<=0 AND is_done(): DONE wins (the `not ledger.is_done()` guard
    on HUNGER_EXPIRED filters this case through to the DONE branch)."""
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState(manually_cleared=True)  # drive=0
    ledger = HungerLedger(task_id="t1", items=[_done_item()])

    snap = engine.tick(policy, clock, ledger)
    assert snap.drive_budget == 0.0
    assert snap.stop_reason is StopReason.DONE


# ---- phase hysteresis --------------------------------------------------------


def test_phase_high_ratio_is_explore() -> None:
    engine = HungerEngine()
    assert (
        engine._phase_with_hysteresis(0.9, previous=None) is LoopPhase.EXPLORE
    )


def test_phase_low_ratio_is_cooldown() -> None:
    engine = HungerEngine()
    assert (
        engine._phase_with_hysteresis(0.1, previous=LoopPhase.EXPLORE)
        is LoopPhase.COOLDOWN
    )


def test_phase_mid_band_with_explore_history_stays_explore() -> None:
    """Hysteresis: if previous was EXPLORE, mid-band keeps EXPLORE.
    Without hysteresis, ratio=0.5 would always be EXPLOIT."""
    engine = HungerEngine()
    assert (
        engine._phase_with_hysteresis(0.5, previous=LoopPhase.EXPLORE)
        is LoopPhase.EXPLORE
    )


def test_phase_mid_band_without_explore_history_is_exploit() -> None:
    engine = HungerEngine()
    assert (
        engine._phase_with_hysteresis(0.5, previous=LoopPhase.COOLDOWN)
        is LoopPhase.EXPLOIT
    )
    # Same band, no previous phase: still EXPLOIT (no upward bias).
    assert (
        engine._phase_with_hysteresis(0.5, previous=None) is LoopPhase.EXPLOIT
    )


# ---- _compute_drive_budget edges --------------------------------------------


def test_manually_cleared_clock_returns_zero_budget() -> None:
    engine = HungerEngine()
    policy = _fresh_policy()
    clock = HungerClockState(manually_cleared=True)
    assert (
        engine._compute_drive_budget(policy, clock, datetime.now(timezone.utc))
        == 0.0
    )


def test_linear_decay_with_no_started_at_returns_initial() -> None:
    """LINEAR needs `started_at`; without it, fall through to `initial_hunger`."""
    engine = HungerEngine()
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.LINEAR,
        decay_duration_seconds=100.0,
        started_at=None,
    )
    clock = HungerClockState()
    assert (
        engine._compute_drive_budget(policy, clock, datetime.now(timezone.utc))
        == 100.0
    )


def test_linear_decay_at_50_percent_elapsed() -> None:
    engine = HungerEngine()
    now = datetime.now(timezone.utc)
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.LINEAR,
        decay_duration_seconds=100.0,
        started_at=now - timedelta(seconds=50),
    )
    clock = HungerClockState()
    budget = engine._compute_drive_budget(policy, clock, now)
    assert 40.0 < budget < 60.0
