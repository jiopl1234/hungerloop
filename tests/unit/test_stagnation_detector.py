"""Unit tests for StagnationDetector service."""
from unittest.mock import MagicMock

from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    HungerItemType,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.tracing import LoopTrace
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
    repo.get_hunger_ledger.return_value = HungerLedger(
        task_id="t1",
        items=list(items.values()),
    )
    repo.increment_no_progress_streak.return_value = 1
    repo.list_loop_traces.return_value = []
    return repo


def _trace(
    loop_id: int,
    *,
    committed: bool,
    newly: list[str] | None = None,
) -> LoopTrace:
    return LoopTrace(
        task_id="t1",
        loop_id=loop_id,
        phase="work",
        active_hunger=1.0,
        drive_budget=1.0,
        work_pressure=1.0,
        candidate_state_id=f"CAND-t1-{loop_id}",
        committed=committed,
        newly_passed_check_keys=newly or [],
    )


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
    detector.update("t1", 1, report, candidate_committed=True)

    assert h1.consecutive_failure_count == 0


def test_no_progress_increments_failure_count() -> None:
    h1 = _item("H-001", failures=1)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(attempted=["H-001"])
    detector.update("t1", 1, report, candidate_committed=True)

    assert h1.consecutive_failure_count == 2


def test_max_failures_blocks_item() -> None:
    h1 = _item("H-001", failures=2)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(repo, max_item_consecutive_failures=3)

    report = _report(attempted=["H-001"])
    result = detector.update("t1", 1, report, candidate_committed=True)

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
    detector.update("t1", 1, report, candidate_committed=True)

    assert h2.consecutive_failure_count == 0
    repo.get_hunger_ledger.assert_called_once_with("t1")


def test_rejected_candidate_new_check_does_not_reset_stagnation() -> None:
    h1 = _item("H-001", failures=2)
    repo = _mock_repo({"H-001": h1})
    detector = StagnationDetector(
        repo,
        max_item_consecutive_failures=10,
        max_global_no_progress_loops=5,
    )
    report = _report(
        attempted=["H-001"],
        newly_passed=["H-001:0"],
        has_progress=True,
    )

    result = detector.update(
        "t1",
        14,
        report,
        candidate_committed=False,
    )

    assert h1.consecutive_failure_count == 3
    assert h1.last_progress_loop_id is None
    repo.reset_no_progress_streak.assert_not_called()
    repo.increment_no_progress_streak.assert_called_once_with("t1")
    assert result["global_blocked"] is False


def test_global_streak_increments_when_rejected_newly_growth_continues() -> None:
    """A rejected candidate always increments the no-progress streak, even
    when its newly-passed count strictly out-passes every rejected loop
    since the last commit. Rejected progress never holds the streak
    (the momentum-hold fuse was removed)."""
    h1 = _item("H-001")
    repo = _mock_repo({"H-001": h1})
    repo.list_loop_traces.return_value = [
        _trace(1, committed=True, newly=["H-001:0"]),
        _trace(2, committed=False, newly=[]),
        _trace(3, committed=False, newly=["H-001:1", "H-001:2"]),
    ]
    detector = StagnationDetector(repo, max_global_no_progress_loops=5)
    report = _report(
        attempted=["H-001"],
        newly_passed=["H-001:1", "H-001:2", "H-001:3"],
    )

    result = detector.update("t1", 4, report, candidate_committed=False)

    repo.increment_no_progress_streak.assert_called_once_with("t1")
    repo.reset_no_progress_streak.assert_not_called()
    assert result["global_blocked"] is False
    assert result["no_progress_streak"] == 1


def test_global_streak_increments_on_first_rejected_newly_after_commit() -> None:
    """A single rejected loop with newly-passed checks is not yet a growth
    trend; the existing invariant (rejected progress does not reset the
    streak) stays intact."""
    h1 = _item("H-001")
    repo = _mock_repo({"H-001": h1})
    repo.list_loop_traces.return_value = [
        _trace(1, committed=True, newly=["H-001:0"]),
    ]
    detector = StagnationDetector(repo, max_global_no_progress_loops=5)
    report = _report(attempted=["H-001"], newly_passed=["H-001:1", "H-001:2"])

    result = detector.update("t1", 2, report, candidate_committed=False)

    repo.increment_no_progress_streak.assert_called_once_with("t1")
    assert result["no_progress_streak"] == 1


def test_global_streak_increments_when_newly_count_plateaus() -> None:
    """Equal newly-passed counts across rejections are a treadmill, not
    momentum — the fuse must keep counting."""
    h1 = _item("H-001")
    repo = _mock_repo({"H-001": h1})
    repo.list_loop_traces.return_value = [
        _trace(1, committed=False, newly=["H-001:1", "H-001:2"]),
    ]
    detector = StagnationDetector(repo, max_global_no_progress_loops=5)
    report = _report(attempted=["H-001"], newly_passed=["H-001:2", "H-001:3"])

    result = detector.update("t1", 2, report, candidate_committed=False)

    repo.increment_no_progress_streak.assert_called_once_with("t1")
    assert result["global_blocked"] is False
    assert result["no_progress_streak"] == 1


def test_rejected_newly_window_restarts_at_last_commit() -> None:
    """Rejected-loop newly counts from before the last commit are irrelevant
    to the streak: a rejected candidate always increments the streak
    regardless of newly-passed growth (the momentum-hold fuse was removed)."""
    h1 = _item("H-001")
    repo = _mock_repo({"H-001": h1})
    repo.list_loop_traces.return_value = [
        _trace(1, committed=False, newly=["H-001:1", "H-001:2", "H-001:3"]),
        _trace(2, committed=True, newly=["H-001:0"]),
        _trace(3, committed=False, newly=["H-001:4", "H-001:5"]),
    ]
    detector = StagnationDetector(repo, max_global_no_progress_loops=5)
    # A rejected candidate always increments the streak; pre-commit loop
    # history no longer affects the streak calculation.
    report = _report(
        attempted=["H-001"],
        newly_passed=["H-001:4", "H-001:5", "H-001:6"],
    )

    result = detector.update("t1", 4, report, candidate_committed=False)

    repo.increment_no_progress_streak.assert_called_once_with("t1")
    assert result["global_blocked"] is False
    assert result["no_progress_streak"] == 1
