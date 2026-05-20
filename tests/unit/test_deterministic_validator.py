from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemType,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validators.deterministic_validator import DeterministicValidator

TASK_ID = "task-1"
LOOP_ID = 7


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-7",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref="candidates/loop_007",
        evidence_ids=["candidate-ev"],
    )


def _item(item_id: str, path: str) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": path},
                description=f"{path} exists",
            )
        ],
    )


def _gate(
    *,
    best: BestState | None = None,
    target_items: list[HungerItem] | None = None,
    regression_items: list[HungerItem] | None = None,
) -> tuple[ValidationGate, MagicMock]:
    runner = MagicMock(spec=AcceptanceCheckRunner)
    runner.run = AsyncMock(return_value=(True, "passed", "runner-ev"))

    repo = MagicMock()
    repo.get_best_state.return_value = best
    repo.get_hunger_items.return_value = target_items or [_item("H-001", "report.md")]
    repo.get_items_for_check_keys.return_value = regression_items or []
    return ValidationGate(repo=repo, acceptance_runner=runner), runner


async def test_wrapper_parity_with_validation_gate() -> None:
    raw_gate, _raw_runner = _gate()
    wrapper_gate, _wrapper_runner = _gate()

    raw_report = await raw_gate.validate(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )
    wrapped_report = await DeterministicValidator(wrapper_gate).validate(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert wrapped_report == raw_report


async def test_targeted_validation_preserved_by_wrapper() -> None:
    best = BestState(
        task_id=TASK_ID,
        state_id="BEST-task-1",
        summary="baseline",
        accepted_check_keys=["H-002:0", "H-999:0"],
    )
    target = _item("H-001", "target.md")
    regression = _item("H-002", "regression.md")
    gate, runner = _gate(
        best=best,
        target_items=[target],
        regression_items=[regression],
    )

    report = await DeterministicValidator(gate).validate(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.attempted_hunger_item_ids == ["H-001"]
    assert {result.check_key for result in report.check_results} == {
        "H-001:0",
        "H-002:0",
    }
    assert "H-999:0" in report.currently_passed_check_keys
    assert runner.run.await_count == 2
