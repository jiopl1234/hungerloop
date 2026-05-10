from hungerloop.models.enums import (
    AcceptanceCheckType,
    CompletionMode,
    HungerItemStatus,
    HungerItemType,
    LoopPhase,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)


def _make_item(
    item_id: str = "H-001",
    status: HungerItemStatus = HungerItemStatus.OPEN,
    gap_score: float = 1.0,
    priority: float = 1.0,
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title="Test item",
        item_type=HungerItemType.GOAL_GAP,
        priority=priority,
        gap_score=gap_score,
        status=status,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
                description="File exists",
            )
        ],
        acceptance_mode="all",
    )


def test_acceptance_check_construction() -> None:
    check = AcceptanceCheck(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": "report.md"},
        description="Report file exists",
    )
    assert check.check_type == AcceptanceCheckType.FILE_EXISTS
    assert check.params["path"] == "report.md"


def test_hunger_item_defaults() -> None:
    item = _make_item()
    assert item.consecutive_failure_count == 0
    assert item.last_progress_loop_id is None
    assert item.evidence_ids == []
    assert item.updated_at_loop == 0
    assert item.refinement_tier == 0
    assert item.refinement_kind == "correctness"
    assert item.generated_by is None
    assert item.source_check_keys == []


def test_hunger_policy_defaults() -> None:
    policy = HungerPolicy(
        initial_hunger=100.0,
        h_max=100.0,
    )
    assert policy.max_total_cost_usd == 10.0
    assert policy.max_total_tokens == 1_000_000
    assert policy.completion_mode is CompletionMode.STOP_ON_DONE
    assert policy.refinement_profile is None
    assert policy.max_refinement_tier == 0
    assert policy.respect_stagnation is True


def test_hunger_ledger_tier_helpers() -> None:
    base = _make_item("H-001", status=HungerItemStatus.VALIDATED_SATISFIED, gap_score=0)
    refinement = _make_item("H-REF-01-tests")
    refinement.refinement_tier = 1
    ledger = HungerLedger(task_id="t1", items=[base, refinement])

    assert ledger.tier_is_done(0) is True
    assert ledger.tier_is_done(1) is False
    assert ledger.all_tiers_done(0) is True
    assert ledger.all_tiers_done(1) is False
    assert ledger.unfinished_items_by_tier(0) == []
    assert ledger.unfinished_items_by_tier(1) == [refinement]


def test_hunger_clock_defaults() -> None:
    clock = HungerClockState()
    assert clock.loop_count == 0
    assert clock.frozen is False
    assert clock.consumed_by_cost_usd == 0.0


def test_hunger_snapshot_construction() -> None:
    snap = HungerSnapshot(
        drive_budget=80.0,
        work_pressure=60.0,
        active_hunger=60.0,
        drive_ratio=0.8,
        phase=LoopPhase.EXPLORE,
        should_stop=False,
        stop_reason=None,
    )
    assert snap.active_hunger == 60.0
    assert snap.phase == LoopPhase.EXPLORE
    assert snap.should_stop is False
