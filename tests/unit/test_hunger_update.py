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


def test_epsilon_snap_to_zero_after_repeated_decrements() -> None:
    """PRD §14: residual ~1e-17 after fractional decrements must snap to 0.0."""
    h1 = _item("H-001", num_checks=3, gap=1.0)
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    # Three rounds of single-check progress; 1.0 - 3 * (1/3) leaves a float dust.
    for i in range(3):
        report = _report(
            ValidationVerdict.PARTIAL,
            newly_passed=[f"H-001:{i}"],
            satisfied=[],
        )
        svc.apply_validation("t1", report)

    assert h1.gap_score == 0.0
    assert h1.status == HungerItemStatus.VALIDATED_SATISFIED


def test_satisfied_force_zeroes_gap_even_if_decrement_falls_short() -> None:
    """PRD §14.3: items in satisfied_hunger_item_ids are force-zeroed."""
    h1 = _item("H-001", num_checks=4, gap=0.75)
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    # Only one new check, but validator says the whole item is satisfied.
    report = _report(
        ValidationVerdict.PASS,
        newly_passed=["H-001:0"],
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


def test_subepsilon_gap_snaps_to_zero_and_satisfies() -> None:
    """PRD §14: float-arithmetic residue (e.g. 5e-10) must snap to 0.0
    so the item flips to VALIDATED_SATISFIED. Without the EPSILON snap
    a multi-loop fractional decrement leaves the item stuck in WORKING.
    """
    h1 = _item("H-001", num_checks=4, gap=5e-10)
    repo = MagicMock()
    repo.get_hunger_item.return_value = h1

    svc = HungerUpdateService(repo)
    # One newly-passed check; the proportional decrement (1/4 = 0.25)
    # would already drive gap to 0, but the test still exercises the
    # `gap_score <= EPSILON -> 0.0` snap branch on the residue.
    report = _report(
        ValidationVerdict.PASS,
        newly_passed=["H-001:0"],
        satisfied=[],  # NOT in satisfied list — exercises the snap path
    )
    svc.apply_validation("t1", report)

    assert h1.gap_score == 0.0
    assert h1.status == HungerItemStatus.VALIDATED_SATISFIED
