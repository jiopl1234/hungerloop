from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus, HungerItemType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger


def _item(
    item_id: str,
    status: HungerItemStatus = HungerItemStatus.OPEN,
    gap: float = 1.0,
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        priority=1.0,
        gap_score=gap,
        status=status,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "x.md"},
                description="x",
            )
        ],
    )


def test_all_blocked_is_not_done() -> None:
    ledger = HungerLedger(
        task_id="t1",
        items=[
            _item("H-001", HungerItemStatus.BLOCKED),
            _item("H-002", HungerItemStatus.BLOCKED),
        ],
    )
    assert ledger.all_remaining_items_blocked() is True
    assert ledger.is_done() is False


def test_all_satisfied_is_done() -> None:
    ledger = HungerLedger(
        task_id="t1",
        items=[
            _item("H-001", HungerItemStatus.VALIDATED_SATISFIED, gap=0.0),
            _item("H-002", HungerItemStatus.CLOSED, gap=0.0),
        ],
    )
    assert ledger.is_done() is True
    assert ledger.all_remaining_items_blocked() is False


def test_mixed_blocked_and_active_is_not_all_blocked() -> None:
    ledger = HungerLedger(
        task_id="t1",
        items=[
            _item("H-001", HungerItemStatus.BLOCKED),
            _item("H-002", HungerItemStatus.OPEN),
        ],
    )
    assert ledger.all_remaining_items_blocked() is False
    assert ledger.has_active_items() is True


def test_blocked_items_excluded_from_work_pressure() -> None:
    ledger = HungerLedger(
        task_id="t1",
        items=[
            _item("H-001", HungerItemStatus.BLOCKED),
            _item("H-002", HungerItemStatus.OPEN),
        ],
    )
    assert ledger.work_pressure() == 1.0


def test_empty_ledger_is_done() -> None:
    ledger = HungerLedger(task_id="t1", items=[])
    assert ledger.is_done() is True
    assert ledger.all_remaining_items_blocked() is False
