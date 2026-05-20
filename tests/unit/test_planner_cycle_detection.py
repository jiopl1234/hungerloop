"""Cycle detection tests for the v0.6 MissionPlanner."""
from __future__ import annotations

from datetime import datetime

import pytest

from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerSnapshot
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.mission_planner import MissionPlanner, PlannerCycleError


def _snapshot() -> HungerSnapshot:
    return HungerSnapshot(
        drive_budget=100.0,
        work_pressure=10.0,
        active_hunger=90.0,
        drive_ratio=0.9,
        phase=LoopPhase.EXPLORE,
        should_stop=False,
    )


def _feature(
    feature_id: str,
    hunger_item_id: str,
    *,
    preconditions: list[str] | None = None,
) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id="phase-1",
        title=f"Feature {feature_id}",
        description=f"Implement {feature_id}",
        preconditions=preconditions or [],
        expected_behavior=[],
        verification_steps=[],
        fulfills=[],
    )


def _mission(features: list[MissionFeature]) -> Mission:
    return Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission title",
        description="Mission description",
        phases=[
            MissionPhase(
                phase_id="phase-1",
                title="Phase 1",
                description="Build phase 1",
                feature_ids=[feature.feature_id for feature in features],
                validation_contract_ids=[],
                status="in_progress",
            )
        ],
        features=features,
        created_at=datetime(2026, 5, 19, 12, 0, 0),
        max_parallel_features=2,
    )


def test_planner_raises_cycle_error() -> None:
    repo = InMemoryRepository()
    features = [
        _feature("F-A", "H-A", preconditions=["F-B"]),
        _feature("F-B", "H-B", preconditions=["F-A"]),
    ]
    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(id="H-A", title="A"),
                HungerItem(id="H-B", title="B"),
            ],
        ),
    )

    with pytest.raises(PlannerCycleError) as exc_info:
        MissionPlanner(repo).plan(
            "task-1",
            1,
            _snapshot(),
            BudgetAllocation(phase=LoopPhase.EXPLORE, max_workers_per_loop=2),
            mission=_mission(features),
        )

    assert set(exc_info.value.cycle) == {"F-A", "F-B"}
    assert "F-A" in str(exc_info.value)
    assert "F-B" in str(exc_info.value)
