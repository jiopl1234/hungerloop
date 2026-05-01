"""Unit tests for HungerUpdateService."""
from unittest.mock import MagicMock

from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    HungerItemType,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.validation import ValidationReport
from hungerloop.services.hunger_update import HungerUpdateService


def _item(item_id: str, num_checks: int = 2, gap: float = 1.0) -> HungerItem:
    checks = [
        AcceptanceCheck(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": f"f{i}.md"},
            description=f"check {i}",
        )
        for i in range(num_checks)
    ]
    return HungerItem(
        id=item_id,
        title="Test",
        item_type=HungerItemType.GOAL_GAP,
        gap_score=gap,
        acceptance_checks=checks,
    )


def _report(
    verdict: ValidationVerdict,
    newly_passed: list[str],
    satisfied: list[str] | None = None,
) -> ValidationReport:
    return ValidationReport(
        id="VAL-t1-1",
        task_id="t1",
        loop_id=1,
        candidate_state_id="CAND-t1-1",
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed,
        satisfied_hunger_item_ids=satisfied or [],
        evidence_ids=["ev-1"],
        has_real_progress=bool(newly_passed),
    )


def test_partial_progress_decrements_gap() -> None:
    h1 = _item("H-001", num_checks=2)
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    report = _report(ValidationVerdict.PARTIAL, newly_passed=["H-001:0"])
    svc.apply_validation("t1", report)

    assert h1.gap_score == 0.5


def test_full_progress_sets_validated_satisfied() -> None:
    h1 = _item("H-001", num_checks=2, gap=0.5)
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    report = _report(
        ValidationVerdict.PASS,
        newly_passed=["H-001:1"],
        satisfied=["H-001"],
    )
    svc.apply_validation("t1", report)

    assert h1.gap_score == 0.0
    assert h1.status == HungerItemStatus.VALIDATED_SATISFIED


def test_fail_verdict_no_update() -> None:
    h1 = _item("H-001")
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    report = _report(ValidationVerdict.FAIL, newly_passed=[])
    svc.apply_validation("t1", report)

    assert h1.gap_score == 1.0
    repo.save_hunger_item.assert_not_called()
