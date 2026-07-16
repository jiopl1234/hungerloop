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
from hungerloop.services.context_builder import (
    DEGRADED_FAILURE_LINE_CHARS,
    ContextBuilder,
    _annotate_workspace_files,
    _apply_history_cap,
)
from hungerloop.services.workspace_reader import WorkspaceReader


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


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_latest_handoff_processing_result.return_value = None
    repo.get_last_worker_result.return_value = None
    repo.list_loop_traces.return_value = []
    repo.list_successful_tool_call_evidence.return_value = []
    return repo


def test_pack_carries_typed_budget(
    budget: BudgetAllocation,
    fake_workspace_reader: WorkspaceReader,
) -> None:
    """ContextPack.budget is the BudgetAllocation instance, not a dict."""
    repo = _repo()
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(
        repo=repo,
        workspace_reader=fake_workspace_reader,
    ).build_for_agent(
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


def test_acceptance_criteria_flatten_in_order(
    budget: BudgetAllocation,
    fake_workspace_reader: WorkspaceReader,
) -> None:
    """Criteria preserve item order then check order, and now carry the
    check's ``params`` payload so the worker has the machine-checkable
    semantics (not only the human description)."""
    repo = _repo()
    repo.get_hunger_items.return_value = [
        _item("H-001", ["alpha", "beta"]),
        _item("H-002", ["gamma"]),
    ]

    pack = ContextBuilder(
        repo=repo,
        workspace_reader=fake_workspace_reader,
    ).build_for_agent(
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

    # Order preserved (item then check).
    assert len(pack.acceptance_criteria) == 3
    assert pack.acceptance_criteria[0].startswith("alpha [")
    assert pack.acceptance_criteria[1].startswith("beta [")
    assert pack.acceptance_criteria[2].startswith("gamma [")
    # Each line carries the original params blob.
    assert '"path": "alpha.md"' in pack.acceptance_criteria[0]
    assert '"path": "beta.md"' in pack.acceptance_criteria[1]
    assert '"path": "gamma.md"' in pack.acceptance_criteria[2]


def test_best_state_summary_none_when_absent(
    budget: BudgetAllocation,
    fake_workspace_reader: WorkspaceReader,
) -> None:
    repo = _repo()
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(
        repo=repo,
        workspace_reader=fake_workspace_reader,
    ).build_for_agent(
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


def test_best_state_summary_passed_through(
    budget: BudgetAllocation,
    fake_workspace_reader: WorkspaceReader,
) -> None:
    repo = _repo()
    repo.get_best_state.return_value = BestState(
        task_id="t1",
        state_id="prev",
        summary="prior summary",
    )
    repo.get_hunger_items.return_value = [_item("H-001", ["a"])]

    pack = ContextBuilder(
        repo=repo,
        workspace_reader=fake_workspace_reader,
    ).build_for_agent(
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


def test_history_cap_degrades_failures_before_dropping() -> None:
    lines = [
        f"loop {i}: H-{i}:0 shell_exit_zero → " + "x" * 2900 for i in range(10)
    ]
    (_, _, _, _, out_failures, info) = _apply_history_cap(
        loop_id=11,
        prior_handoff_summary="",
        last_summary=None,
        best_summary=None,
        best_files=[],
        evidence_ids=[],
        evidence_lines=[],
        failure_lines=list(lines),
        best_summary_truncated=False,
    )
    assert info is not None
    assert info.degraded_failures > 0
    # Oldest lines got degraded to one-liners instead of being dropped.
    assert any(len(line) <= DEGRADED_FAILURE_LINE_CHARS for line in out_failures)
    # The newest line keeps its full detail.
    assert len(out_failures[0]) > DEGRADED_FAILURE_LINE_CHARS
    # Nothing was dropped outright in this scenario.
    assert info.dropped_failures == 0
    assert len(out_failures) == 10


def test_annotate_workspace_files() -> None:
    assert _annotate_workspace_files([("a.py", 2048, 100)]) == ["a.py (100L, 2.0KB)"]
    assert _annotate_workspace_files([("img.png", 500, -1)]) == ["img.png (500B)"]
