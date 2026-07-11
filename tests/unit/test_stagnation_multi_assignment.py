"""M3 stagnation semantics for multi-assignment loops."""
from __future__ import annotations

from unittest.mock import MagicMock

from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.hunger import HungerItem, HungerLedger
from hungerloop.models.validation import ValidationReport
from hungerloop.services.stagnation_detector import StagnationDetector


def _report(
    *,
    attempted: list[str] | None = None,
    newly_passed: list[str] | None = None,
) -> ValidationReport:
    return ValidationReport(
        id="VAL-task-1-1",
        task_id="task-1",
        loop_id=1,
        candidate_state_id="CAND-task-1-1",
        baseline_state_id=None,
        verdict=ValidationVerdict.FAIL,
        attempted_hunger_item_ids=attempted or [],
        newly_passed_check_keys=newly_passed or [],
        has_real_progress=bool(newly_passed),
    )


def _repo_with_items(items: list[HungerItem]) -> MagicMock:
    repo = MagicMock()
    repo.get_hunger_ledger.return_value = HungerLedger(
        task_id="task-1",
        items=items,
    )
    repo.increment_no_progress_streak.return_value = 1
    return repo


def test_attempted_union() -> None:
    """The orchestrator can pass the union attempted by completed assignments."""
    h1 = HungerItem(id="H-001", title="First")
    h2 = HungerItem(id="H-002", title="Second")
    repo = _repo_with_items([h1, h2])

    StagnationDetector(repo).update(
        "task-1",
        1,
        _report(attempted=[]),
        candidate_committed=True,
        attempted_hunger_item_ids=["H-001", "H-002"],
    )

    assert h1.consecutive_failure_count == 1
    assert h2.consecutive_failure_count == 1


def test_skipped_not_attempted() -> None:
    """Skipped downstream assignments must not increment failure counters."""
    h1 = HungerItem(id="H-001", title="Attempted")
    h2 = HungerItem(id="H-002", title="Skipped")
    repo = _repo_with_items([h1, h2])

    StagnationDetector(repo).update(
        "task-1",
        1,
        _report(attempted=["H-001", "H-002"]),
        candidate_committed=True,
        attempted_hunger_item_ids=["H-001"],
    )

    assert h1.consecutive_failure_count == 1
    assert h2.consecutive_failure_count == 0
