"""Unit tests for the v0.6 MissionPlanner (REQ-M3-010..020)."""
from __future__ import annotations

import re
from datetime import datetime
from unittest.mock import patch

import pytest

from hungerloop.models.enums import HungerItemStatus, LoopPhase
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerSnapshot
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation, LoopPlan
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.agent_registry import EXECUTION_WORKER_V1_ID
from hungerloop.services.mission_planner import MissionPlanner
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


def _budget(
    *,
    max_workers_per_loop: int = 3,
    max_assignment_retries: int = 1,
) -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        max_workers_per_loop=max_workers_per_loop,
        max_assignment_retries=max_assignment_retries,
    )


def _phase(
    phase_id: str = "phase-1",
    *,
    feature_ids: list[str] | None = None,
    status: str = "in_progress",
) -> MissionPhase:
    return MissionPhase(
        phase_id=phase_id,
        title=f"Phase {phase_id}",
        description=f"Build {phase_id}",
        feature_ids=feature_ids or [],
        validation_contract_ids=[],
        status=status,  # type: ignore[arg-type]
    )


def _feature(
    feature_id: str,
    hunger_item_id: str,
    *,
    phase_id: str = "phase-1",
    status: str = "pending",
    preconditions: list[str] | None = None,
    verification_steps: list[str] | None = None,
) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id=phase_id,
        title=f"Feature {feature_id}",
        description=f"Implement {feature_id}",
        preconditions=preconditions or [],
        expected_behavior=[f"{feature_id} works"],
        verification_steps=verification_steps or [],
        fulfills=[],
        status=status,  # type: ignore[arg-type]
    )


def _mission(
    features: list[MissionFeature],
    *,
    phases: list[MissionPhase] | None = None,
    max_parallel_features: int | None = 3,
) -> Mission:
    phase_list = phases or [
        _phase(
            "phase-1",
            feature_ids=[feature.feature_id for feature in features],
        )
    ]
    return Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission title",
        description="Mission description",
        phases=phase_list,
        features=features,
        created_at=datetime(2026, 5, 19, 12, 0, 0),
        max_parallel_features=max_parallel_features,
    )


def _item(
    item_id: str,
    *,
    priority: float = 1.0,
    gap_score: float = 1.0,
    refinement_tier: int = 0,
    status: HungerItemStatus = HungerItemStatus.OPEN,
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        priority=priority,
        gap_score=gap_score,
        refinement_tier=refinement_tier,
        status=status,
    )


def _save_ledger(repo: InMemoryRepository, items: list[HungerItem]) -> None:
    repo.save_hunger_ledger("task-1", HungerLedger(task_id="task-1", items=items))


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


def test_no_mission_falls_back_to_v0_5f(repo: InMemoryRepository) -> None:
    expected = LoopPlan(
        task_id="task-1",
        loop_id=7,
        selected_hunger_item_ids=[],
        assignments=[],
        phase=LoopPhase.EXPLORE,
        rationale="legacy",
    )

    with patch.object(RuleBasedPlanner, "plan", return_value=expected) as legacy_plan:
        plan = MissionPlanner(repo).plan(
            "task-1",
            7,
            _snapshot(),
            _budget(max_workers_per_loop=5),
            mission=None,
        )

    assert plan is expected
    legacy_plan.assert_called_once()
    assert legacy_plan.call_args.args == (
        "task-1",
        7,
        _snapshot(),
        _budget(max_workers_per_loop=5),
    )


def test_plan_three_independent_features(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-low", "H-low"),
        _feature("F-high", "H-high"),
        _feature("F-mid", "H-mid"),
    ]
    _save_ledger(
        repo,
        [
            _item("H-low", priority=0.5, gap_score=1.0),
            _item("H-high", priority=0.9, gap_score=1.0),
            _item("H-mid", priority=0.7, gap_score=1.0),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        3,
        _snapshot(),
        _budget(max_workers_per_loop=3),
        mission=_mission(features, max_parallel_features=3),
    )

    assert [a.target_feature_ids for a in plan.assignments] == [
        ["F-high"],
        ["F-mid"],
        ["F-low"],
    ]
    assert [a.role for a in plan.assignments] == ["executor", "executor", "executor"]
    assert {a.agent_id for a in plan.assignments} == {EXECUTION_WORKER_V1_ID}
    assert plan.selected_hunger_item_ids == ["H-high", "H-mid", "H-low"]


def test_cap_min_of_three_bounds(repo: InMemoryRepository) -> None:
    features = [_feature(f"F-{index}", f"H-{index}") for index in range(5)]
    _save_ledger(repo, [_item(f"H-{index}") for index in range(5)])

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=10),
        mission=_mission(features, max_parallel_features=2),
    )

    assert len(plan.assignments) == 2


def test_m_less_than_one_returns_empty_before_dependency_analysis(
    repo: InMemoryRepository,
) -> None:
    features = [
        _feature("F-A", "H-A", preconditions=["F-B"]),
        _feature("F-B", "H-B", preconditions=["F-A"]),
    ]
    _save_ledger(repo, [_item("H-A"), _item("H-B")])
    zero_worker_budget = BudgetAllocation.model_construct(
        phase=LoopPhase.EXPLORE,
        max_workers_per_loop=0,
        max_assignment_retries=1,
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        zero_worker_budget,
        mission=_mission(features, max_parallel_features=3),
    )

    assert plan.assignments == []
    assert plan.selected_hunger_item_ids == []
    assert "not_selected_budget_cap" in plan.rationale


def test_empty_plan_when_no_candidates(repo: InMemoryRepository) -> None:
    feature = _feature("F-done", "H-done", status="done")
    _save_ledger(repo, [_item("H-done")])

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=3),
        mission=_mission([feature]),
    )

    assert plan.assignments == []
    assert plan.selected_hunger_item_ids == []
    assert "no candidate features" in plan.rationale
    assert "F-done" in plan.rationale
    assert "feature_status_done" in plan.rationale


@pytest.mark.parametrize(
    "inactive_status",
    [
        HungerItemStatus.BLOCKED,
        HungerItemStatus.PAUSED,
        HungerItemStatus.CLOSED,
        HungerItemStatus.VALIDATED_SATISFIED,
    ],
)
def test_filter_inactive_hunger_status(
    repo: InMemoryRepository,
    inactive_status: HungerItemStatus,
) -> None:
    features = [
        _feature("F-active", "H-active"),
        _feature("F-inactive", "H-inactive"),
    ]
    _save_ledger(
        repo,
        [
            _item("H-active"),
            _item("H-inactive", status=inactive_status),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=3),
        mission=_mission(features),
    )

    assert [a.target_feature_ids for a in plan.assignments] == [["F-active"]]
    assert "F-inactive" in plan.rationale
    assert f"hunger_status_{inactive_status.value}" in plan.rationale


def test_candidate_filter_excludes_status_phase_and_handoff_blockers(
    repo: InMemoryRepository,
) -> None:
    features = [
        _feature("F-active", "H-active", phase_id="phase-open"),
        _feature("F-done", "H-done", phase_id="phase-open", status="done"),
        _feature("F-blocked", "H-blocked", phase_id="phase-open", status="blocked"),
        _feature("F-validating", "H-validating", phase_id="phase-validating"),
        _feature("F-prior-blocker", "H-prior-blocker", phase_id="phase-open"),
    ]
    phases = [
        _phase(
            "phase-open",
            feature_ids=[
                "F-active",
                "F-done",
                "F-blocked",
                "F-prior-blocker",
            ],
        ),
        _phase(
            "phase-validating",
            feature_ids=["F-validating"],
            status="validating",
        ),
    ]
    _save_ledger(
        repo,
        [
            _item("H-active"),
            _item("H-done"),
            _item("H-blocked"),
            _item("H-validating"),
            _item("H-prior-blocker"),
        ],
    )
    prior_handoff = WorkerHandoff(
        agent_id=EXECUTION_WORKER_V1_ID,
        task_id="task-1",
        loop_id=1,
        handoff_items=[
            HandoffItem(
                item_type="blocker",
                summary="Blocked",
                related_feature_ids=["F-prior-blocker"],
            )
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        2,
        _snapshot(),
        _budget(max_workers_per_loop=5),
        mission=_mission(features, phases=phases, max_parallel_features=5),
        prior_handoffs=[prior_handoff],
    )

    assert [a.target_feature_ids for a in plan.assignments] == [["F-active"]]
    assert "F-done" in plan.rationale
    assert "F-blocked" in plan.rationale
    assert "F-validating" in plan.rationale
    assert "F-prior-blocker" in plan.rationale
    assert "blocked_by_prior_handoff" in plan.rationale


def test_sort_order_tier_then_score(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-X", "H-X"),
        _feature("F-Y", "H-Y"),
        _feature("F-Z", "H-Z"),
    ]
    _save_ledger(
        repo,
        [
            _item("H-X", priority=9.0, gap_score=1.0, refinement_tier=2),
            _item("H-Y", priority=4.0, gap_score=1.0, refinement_tier=1),
            _item("H-Z", priority=8.0, gap_score=1.0, refinement_tier=1),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=3),
        mission=_mission(features, max_parallel_features=3),
    )

    assert [a.target_feature_ids[0] for a in plan.assignments] == [
        "F-Z",
        "F-Y",
        "F-X",
    ]


def test_assignment_field_population(repo: InMemoryRepository) -> None:
    feature = _feature("F-1", "H-1")
    _save_ledger(repo, [_item("H-1")])

    plan = MissionPlanner(repo).plan(
        "task-1",
        42,
        _snapshot(),
        _budget(max_workers_per_loop=1, max_assignment_retries=2),
        mission=_mission([feature]),
    )

    assignment = plan.assignments[0]
    assert assignment.agent_id == EXECUTION_WORKER_V1_ID
    assert assignment.role == "executor"
    assert assignment.target_feature_ids == ["F-1"]
    assert assignment.target_hunger_item_ids == ["H-1"]
    assert assignment.allowed_tools == [
        "read_file",
        "write_file",
        "patch_file",
        "run_shell",
    ]
    assert assignment.max_retries == 2
    assert assignment.retry_count == 0
    assert re.match(r"^ASGN-task-1-42-0$", assignment.assignment_id)
    assert "Mission title" in assignment.mission
    assert "F-1" in assignment.mission


def test_depends_on_from_preconditions(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-A", "H-A"),
        _feature("F-B", "H-B", preconditions=["F-A"]),
    ]
    _save_ledger(repo, [_item("H-A"), _item("H-B")])

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=2),
        mission=_mission(features, max_parallel_features=2),
    )

    by_feature = {assignment.target_feature_ids[0]: assignment for assignment in plan.assignments}
    assert by_feature["F-A"].depends_on == []
    assert by_feature["F-B"].depends_on == [by_feature["F-A"].assignment_id]


def test_cap_prefers_active_prerequisite_before_dependent(
    repo: InMemoryRepository,
) -> None:
    features = [
        _feature("F-A", "H-A"),
        _feature("F-B", "H-B", preconditions=["F-A"]),
    ]
    _save_ledger(
        repo,
        [
            _item("H-A", priority=1.0, gap_score=1.0),
            _item("H-B", priority=9.0, gap_score=1.0),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=1),
        mission=_mission(features, max_parallel_features=3),
    )

    assert [a.target_feature_ids[0] for a in plan.assignments] == ["F-A"]
    assert "F-B" in plan.rationale
    assert "not_selected_budget_cap" in plan.rationale


def test_topology_order(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-A", "H-A"),
        _feature("F-B", "H-B", preconditions=["F-A"]),
        _feature("F-C", "H-C", preconditions=["F-B"]),
    ]
    _save_ledger(
        repo,
        [
            _item("H-A", priority=1.0, gap_score=1.0),
            _item("H-B", priority=5.0, gap_score=1.0),
            _item("H-C", priority=9.0, gap_score=1.0),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=3),
        mission=_mission(features, max_parallel_features=3),
    )

    assert [a.target_feature_ids[0] for a in plan.assignments] == [
        "F-A",
        "F-B",
        "F-C",
    ]
    index_by_assignment = {
        assignment.assignment_id: index
        for index, assignment in enumerate(plan.assignments)
    }
    for assignment in plan.assignments:
        for dependency in assignment.depends_on:
            assert index_by_assignment[dependency] < index_by_assignment[assignment.assignment_id]


def test_plan_is_deterministic(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-A", "H-A"),
        _feature("F-B", "H-B", preconditions=["F-A"]),
        _feature("F-C", "H-C", verification_steps=["Check artifact from F-A"]),
    ]
    _save_ledger(
        repo,
        [
            _item("H-A", priority=1.0, gap_score=1.0),
            _item("H-B", priority=2.0, gap_score=1.0),
            _item("H-C", priority=3.0, gap_score=1.0),
        ],
    )
    mission = _mission(features, max_parallel_features=3)
    planner = MissionPlanner(repo)

    plans = [
        planner.plan(
            "task-1",
            9,
            _snapshot(),
            _budget(max_workers_per_loop=3),
            mission=mission,
        ).model_dump(mode="json")
        for _ in range(10)
    ]

    assert all(plan == plans[0] for plan in plans)


def test_rationale_lines_match_features(repo: InMemoryRepository) -> None:
    features = [
        _feature("F-selected-a", "H-selected-a"),
        _feature("F-selected-b", "H-selected-b"),
        _feature("F-skipped", "H-skipped", status="done"),
    ]
    _save_ledger(
        repo,
        [
            _item("H-selected-a", priority=2.0, refinement_tier=0),
            _item("H-selected-b", priority=1.0, refinement_tier=0),
            _item("H-skipped", priority=9.0, refinement_tier=0),
        ],
    )

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=2),
        mission=_mission(features, max_parallel_features=2),
    )

    lines = plan.rationale.splitlines()
    assert len(lines) == 3
    assert any(
        "selected F-selected-a" in line
        and "priority=2.0" in line
        and "refinement_tier=0" in line
        for line in lines
    )
    assert any("selected F-selected-b" in line for line in lines)
    assert any("skipped F-skipped" in line and "feature_status_done" in line for line in lines)


def test_max_workers_per_loop_one_with_mission(repo: InMemoryRepository) -> None:
    features = [_feature(f"F-{index}", f"H-{index}") for index in range(3)]
    _save_ledger(repo, [_item(f"H-{index}") for index in range(3)])

    plan = MissionPlanner(repo).plan(
        "task-1",
        1,
        _snapshot(),
        _budget(max_workers_per_loop=1),
        mission=_mission(features, max_parallel_features=3),
    )

    assert len(plan.assignments) == 1
