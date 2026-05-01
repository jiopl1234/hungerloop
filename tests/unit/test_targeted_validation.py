"""Unit tests for AcceptanceCheckRunner and ValidationGate."""
from unittest.mock import AsyncMock, MagicMock

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemType,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.validation_gate import ValidationGate, make_check_key


def test_make_check_key() -> None:
    assert make_check_key("H-001", 0) == "H-001:0"
    assert make_check_key("H-002", 1) == "H-002:1"


def _item(item_id: str, checks: list[AcceptanceCheck] | None = None) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=checks
        or [
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
                description="File exists",
            ),
        ],
        acceptance_mode="all",
    )


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-t1-1",
        task_id="t1",
        loop_id=1,
        summary="test",
        workspace_ref="candidates/loop_001",
        evidence_ids=["ev-1"],
    )


async def test_only_target_items_validated() -> None:
    runner = MagicMock(spec=AcceptanceCheckRunner)
    runner.run = AsyncMock(return_value=(True, "pass", None))

    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_hunger_items.return_value = [_item("H-001")]
    repo.get_items_for_check_keys.return_value = []

    gate = ValidationGate(repo=repo, acceptance_runner=runner)
    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.attempted_hunger_item_ids == ["H-001"]
    assert "H-001:0" in report.newly_passed_check_keys


async def test_untargeted_item_not_validated() -> None:
    runner = MagicMock(spec=AcceptanceCheckRunner)
    runner.run = AsyncMock(return_value=(True, "pass", None))

    h1 = _item("H-001")

    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_hunger_items.return_value = [h1]
    repo.get_items_for_check_keys.return_value = []

    gate = ValidationGate(repo=repo, acceptance_runner=runner)
    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    checked_item_ids = {r.hunger_item_id for r in report.check_results}
    assert "H-002" not in checked_item_ids


async def test_previously_passed_checked_as_regression() -> None:
    runner = MagicMock(spec=AcceptanceCheckRunner)
    runner.run = AsyncMock(return_value=(True, "pass", None))

    h1 = _item("H-001")

    repo = MagicMock()
    best = BestState(
        task_id="t1",
        state_id="prev",
        summary="prev",
        accepted_check_keys=["H-001:0"],
    )
    repo.get_best_state.return_value = best
    repo.get_hunger_items.return_value = [_item("H-002")]
    repo.get_items_for_check_keys.return_value = [h1]

    gate = ValidationGate(repo=repo, acceptance_runner=runner)
    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-002"],
    )

    checked_keys = {r.check_key for r in report.check_results}
    assert "H-001:0" in checked_keys


async def test_regression_detected() -> None:
    call_count = 0

    async def mock_run(
        check: AcceptanceCheck,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
    ) -> tuple[bool, str, str | None]:
        nonlocal call_count
        call_count += 1
        if check.params.get("path") == "report.md":
            return (False, "file missing", None)
        return (True, "pass", None)

    runner = MagicMock(spec=AcceptanceCheckRunner)
    runner.run = AsyncMock(side_effect=mock_run)

    h1 = _item("H-001")
    repo = MagicMock()
    best = BestState(
        task_id="t1",
        state_id="prev",
        summary="prev",
        accepted_check_keys=["H-001:0"],
    )
    repo.get_best_state.return_value = best
    repo.get_hunger_items.return_value = [h1]
    repo.get_items_for_check_keys.return_value = [h1]

    gate = ValidationGate(repo=repo, acceptance_runner=runner)
    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert "H-001:0" in report.regressed_check_keys
    assert report.verdict == ValidationVerdict.FAIL
