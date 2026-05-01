from hungerloop.models.enums import (
    AcceptanceCheckType,
    DecayType,
    HungerItemStatus,
    HungerItemType,
    StopReason,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.services.hunger_engine import HungerEngine


def _item(status: HungerItemStatus = HungerItemStatus.OPEN) -> HungerItem:
    return HungerItem(
        id="H-001",
        title="Test",
        item_type=HungerItemType.GOAL_GAP,
        priority=1.0,
        gap_score=1.0,
        status=status,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "x.md"},
                description="x",
            )
        ],
    )


def _policy(max_loops: int = 10) -> HungerPolicy:
    return HungerPolicy(
        initial_hunger=100.0,
        h_max=100.0,
        decay_type=DecayType.LOOP_COUNT,
        decay_duration_seconds=float(max_loops),
    )


def _clock(loop_count: int = 0) -> HungerClockState:
    return HungerClockState(loop_count=loop_count)


def _ledger(items: list[HungerItem] | None = None) -> HungerLedger:
    return HungerLedger(task_id="t1", items=items or [_item()])


def test_loop_0_full_budget() -> None:
    engine = HungerEngine()
    snap = engine.tick(_policy(10), _clock(0), _ledger())
    assert snap.drive_budget == 100.0
    assert snap.should_stop is False


def test_loop_9_last_loop_can_start() -> None:
    engine = HungerEngine()
    snap = engine.tick(_policy(10), _clock(9), _ledger())
    assert snap.drive_budget == 10.0
    assert snap.should_stop is False


def test_loop_10_hunger_expired() -> None:
    engine = HungerEngine()
    snap = engine.tick(_policy(10), _clock(10), _ledger())
    assert snap.drive_budget == 0.0
    assert snap.should_stop is True
    assert snap.stop_reason == StopReason.HUNGER_EXPIRED


def test_all_done_stops_done() -> None:
    engine = HungerEngine()
    done_item = _item(HungerItemStatus.VALIDATED_SATISFIED)
    done_item.gap_score = 0.0
    snap = engine.tick(_policy(10), _clock(0), _ledger([done_item]))
    assert snap.should_stop is True
    assert snap.stop_reason == StopReason.DONE


def test_all_blocked_stops_blocked() -> None:
    engine = HungerEngine()
    snap = engine.tick(
        _policy(10),
        _clock(0),
        _ledger([_item(HungerItemStatus.BLOCKED)]),
    )
    assert snap.should_stop is True
    assert snap.stop_reason == StopReason.BLOCKED


def test_blocked_checked_before_done() -> None:
    """BLOCKED items must not trigger DONE even if work_pressure is 0."""
    engine = HungerEngine()
    snap = engine.tick(
        _policy(10),
        _clock(0),
        _ledger([_item(HungerItemStatus.BLOCKED)]),
    )
    assert snap.stop_reason == StopReason.BLOCKED
    # Redundant after the prior assert, but documents the I-9 invariant
    # (BLOCKED before DONE). mypy narrows so we silence the overlap warning.
    assert snap.stop_reason != StopReason.DONE  # type: ignore[comparison-overlap]


def test_frozen_clock_paused() -> None:
    engine = HungerEngine()
    clock = _clock()
    clock.frozen = True
    snap = engine.tick(_policy(10), clock, _ledger())
    assert snap.should_stop is True
    assert snap.stop_reason == StopReason.HUMAN_PAUSED


def test_cost_exceeded_safety_stop() -> None:
    engine = HungerEngine()
    clock = _clock()
    clock.consumed_by_cost_usd = 100.0
    policy = _policy()
    policy.max_total_cost_usd = 10.0
    snap = engine.tick(policy, clock, _ledger())
    assert snap.should_stop is True
    assert snap.stop_reason == StopReason.SAFETY_STOP
