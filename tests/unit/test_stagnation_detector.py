"""Unit tests for StagnationDetector service."""
from unittest.mock import MagicMock

from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    HungerItemType,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.validation import ValidationReport
from hungerloop.services.stagnation_detector import StagnationDetector


def _item(item_id: str, failures: int = 0) -> HungerItem:
    return HungerItem(
        id=item_id,
        title="Test",
        item_type=HungerItemType.GOAL_GAP,
        consecutive_failure_count=failures,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "f.md"},
                description="check",
            )
        ],
    )


def _mock_repo(items: dict[str, HungerItem]) -> MagicMock:
    repo = MagicMock()
    repo.get_hunger_item.side_effect = lambda iid: items.get(iid)
    repo.increment_no_progress_streak.return_value = 1
    return repo


def _report(
    attempted: list[str],
    newly_passed: list[str] | None = None,
    has_progress: bool = False,
) -> ValidationReport:
    verdict = ValidationVerdict.PARTIAL if has_progress else ValidationVerdict.FAIL
    return ValidationReport(
        id="VAL-t1-1",
        task_id="t1",
        loop_id=1,
        candidate_state_id="CAND-t1-1",
        baseline_state_id=None,
        verdict=verdict,
        attempted_hunger_item_ids=attempted,
        newly_passed_check_keys=newly_passed or [],
        has_real_progress=has_progress,
    )


def test_progress_resets_failure_count() -> None:
    h1 = _item("H-001", failures=2)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(
        attempted=["H-001"],
        newly_passed=["H-001:0"],
        has_progress=True,
    )
    detector.update("t1", 1, report)

    assert h1.consecutive_failure_count == 0


def test_no_progress_increments_failure_count() -> None:
    h1 = _item("H-001", failures=1)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(attempted=["H-001"])
    detector.update("t1", 1, report)

    assert h1.consecutive_failure_count == 2


def test_max_failures_blocks_item() -> None:
    h1 = _item("H-001", failures=2)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(attempted=["H-001"])
    result = detector.update("t1", 1, report)

    assert h1.status == HungerItemStatus.BLOCKED
    blocked = result["blocked_items"]
    assert isinstance(blocked, list)
    assert "H-001" in blocked


def test_unattempted_item_not_counted() -> None:
    h1 = _item("H-001", failures=0)
    h2 = _item("H-002", failures=0)
    repo = _mock_repo({"H-001": h1, "H-002": h2})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(attempted=["H-001"])
    detector.update("t1", 1, report)

    assert h2.consecutive_failure_count == 0
    repo.get_hunger_item.assert_any_call("H-001")
