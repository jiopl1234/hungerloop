"""Unit tests for ContextBuilder (v0.5a; PRD §28.7).

Verifies that ``build_for_agent`` populates a ``ContextPack`` whose ``budget``
is the same ``BudgetAllocation`` instance passed in (no dict construction),
that acceptance criteria flatten in item-then-check order, and that
best_state_summary falls back to ``None`` when no BestState exists.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.context import ContextPack
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemType,
    LoopPhase,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.context_builder import ContextBuilder


def _check(description: str) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": f"{description}.md"},
        description=description,
    )


def _item(item_id: str, descriptions: list[str]) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=[_check(d) for d in descriptions],
    )


@pytest.fixture
def budget() -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLOIT,
        max_tokens=2000,
        max_tool_calls=5,
        max_wall_clock_seconds=120,
    )


def test_pack_carries_typed_budget(budget: BudgetAllocation) -> None:
    """ContextPack.budget is the BudgetAllocation instance, not a dict."""
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(repo=repo).build_for_agent(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="do the thing",
        target_hunger_item_ids=["H-001"],
        budget=budget,
        allowed_tools=["read_file"],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )

    assert isinstance(pack, ContextPack)
    assert isinstance(pack.budget, BudgetAllocation)
    assert pack.budget is budget  # passed by reference, not reconstructed
    assert pack.budget.max_wall_clock_seconds == 120
    assert pack.phase == "exploit"


def test_acceptance_criteria_flatten_in_order(budget: BudgetAllocation) -> None:
    """Criteria preserve item order then check order."""
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_hunger_items.return_value = [
        _item("H-001", ["alpha", "beta"]),
        _item("H-002", ["gamma"]),
    ]

    pack = ContextBuilder(repo=repo).build_for_agent(
        task_id="t1",
        loop_id=1,
        agent_id="a",
        mission="m",
        target_hunger_item_ids=["H-001", "H-002"],
        budget=budget,
        allowed_tools=[],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )

    assert pack.acceptance_criteria == ["alpha", "beta", "gamma"]


def test_best_state_summary_none_when_absent(budget: BudgetAllocation) -> None:
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(repo=repo).build_for_agent(
        task_id="t1",
        loop_id=1,
        agent_id="a",
        mission="m",
        target_hunger_item_ids=["H-001"],
        budget=budget,
        allowed_tools=[],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )
    assert pack.best_state_summary is None


def test_best_state_summary_passed_through(budget: BudgetAllocation) -> None:
    repo = MagicMock()
    repo.get_best_state.return_value = BestState(
        task_id="t1",
        state_id="prev",
        summary="prior summary",
    )
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(repo=repo).build_for_agent(
        task_id="t1",
        loop_id=1,
        agent_id="a",
        mission="m",
        target_hunger_item_ids=["H-001"],
        budget=budget,
        allowed_tools=[],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )
    assert pack.best_state_summary == "prior summary"
