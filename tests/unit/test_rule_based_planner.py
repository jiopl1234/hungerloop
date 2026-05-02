"""Unit tests for RuleBasedPlanner (PRD §5)."""
from __future__ import annotations

import pytest

from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    LoopPhase,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerSnapshot,
)
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.agent_registry import EXECUTION_WORKER_V1_ID
from hungerloop.services.rule_based_planner import RuleBasedPlanner


def _snapshot(phase: LoopPhase = LoopPhase.EXPLORE) -> HungerSnapshot:
    return HungerSnapshot(
        drive_budget=100.0,
        work_pressure=10.0,
        active_hunger=90.0,
        drive_ratio=0.9,
        phase=phase,
        should_stop=False,
    )


def _budget(phase: LoopPhase = LoopPhase.EXPLORE) -> BudgetAllocation:
    return BudgetAllocation(phase=phase)


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


def test_empty_ledger_returns_empty_plan(repo: InMemoryRepository) -> None:
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), _budget())

    assert plan.assignments == []
    assert plan.selected_hunger_item_ids == []
    assert plan.rationale == "No active hunger items available for planning."
    assert plan.task_id == "t1"
    assert plan.loop_id == 1


def test_only_blocked_items_returns_empty_plan(repo: InMemoryRepository) -> None:
    """BLOCKED items are not active; planner should not pick them (PRD §5.5)."""
    blocked = HungerItem(
        id="H-001",
        title="Stuck",
        status=HungerItemStatus.BLOCKED,
        priority=1.0,
        gap_score=1.0,
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[blocked]))
    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), _budget())

    assert plan.assignments == []
    assert plan.rationale == "No active hunger items available for planning."


def test_picks_highest_priority_times_gap_score(repo: InMemoryRepository) -> None:
    items = [
        HungerItem(id="H-001", title="low", priority=0.5, gap_score=1.0),
        HungerItem(id="H-002", title="winner", priority=0.9, gap_score=0.9),
        HungerItem(id="H-003", title="mid", priority=0.7, gap_score=0.5),
    ]
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))

    plan = RuleBasedPlanner(repo).plan("t1", 5, _snapshot(), _budget())

    assert plan.selected_hunger_item_ids == ["H-002"]
    assert len(plan.assignments) == 1
    assert plan.assignments[0].target_hunger_item_ids == ["H-002"]


def test_assignment_routes_to_execution_worker_v1(repo: InMemoryRepository) -> None:
    item = HungerItem(id="H-001", title="Build report", priority=1.0, gap_score=1.0)
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))

    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), _budget())

    assignment = plan.assignments[0]
    assert assignment.agent_id == EXECUTION_WORKER_V1_ID
    assert assignment.allowed_tools == [
        "read_file",
        "write_file",
        "patch_file",
        "run_shell",
    ]


def test_max_workers_per_loop_caps_to_one(repo: InMemoryRepository) -> None:
    """v0.5a is fixed to 1 worker even when budget says more (PRD §28.11)."""
    items = [
        HungerItem(id=f"H-{i:03d}", title=f"item {i}", priority=1.0, gap_score=1.0)
        for i in range(5)
    ]
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))
    budget = BudgetAllocation(phase=LoopPhase.EXPLORE, max_workers_per_loop=4)

    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), budget)

    assert len(plan.assignments) == 1
    assert len(plan.selected_hunger_item_ids) == 1


def test_phase_propagates_from_budget(repo: InMemoryRepository) -> None:
    item = HungerItem(id="H-001", title="x", priority=1.0, gap_score=1.0)
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))

    plan = RuleBasedPlanner(repo).plan(
        "t1", 1, _snapshot(LoopPhase.EXPLORE), _budget(LoopPhase.EXPLOIT)
    )

    assert plan.phase == LoopPhase.EXPLOIT


def test_mission_includes_phase_and_acceptance_checks(repo: InMemoryRepository) -> None:
    item = HungerItem(
        id="H-001",
        title="Build report",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
                description="Report file exists",
            )
        ],
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))

    plan = RuleBasedPlanner(repo).plan(
        "t1", 1, _snapshot(LoopPhase.EXPLOIT), _budget(LoopPhase.EXPLOIT)
    )

    mission = plan.assignments[0].mission
    assert "phase=exploit" in mission
    assert "H-001" in mission
    assert "Build report" in mission
    assert "Report file exists" in mission


def test_mission_falls_back_when_no_acceptance_checks(
    repo: InMemoryRepository,
) -> None:
    item = HungerItem(id="H-001", title="x", priority=1.0, gap_score=1.0)
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))

    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), _budget())

    assert "no acceptance checks" in plan.assignments[0].mission


def test_rationale_names_the_selected_item(repo: InMemoryRepository) -> None:
    item = HungerItem(id="H-042", title="x", priority=1.0, gap_score=1.0)
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))

    plan = RuleBasedPlanner(repo).plan("t1", 1, _snapshot(), _budget())

    assert "H-042" in plan.rationale
