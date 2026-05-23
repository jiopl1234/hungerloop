from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from hungerloop.models.enums import HungerItemStatus, LoopPhase
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerSnapshot
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.mission_planner import MissionPlanner

_TASK_ID = "perf-planner"
_MISSION_ID = "mission-perf-planner"
_PHASE_ID = "phase-1"
_BUDGET_MS = 50.0


def _snapshot() -> HungerSnapshot:
    return HungerSnapshot(
        drive_budget=100.0,
        work_pressure=100.0,
        active_hunger=100.0,
        drive_ratio=1.0,
        phase=LoopPhase.EXPLORE,
        should_stop=False,
    )


def _repo_and_mission(feature_count: int = 100) -> tuple[InMemoryRepository, Mission]:
    repo = InMemoryRepository()
    repo.create_task(_TASK_ID, "mission planner perf")
    features = [
        MissionFeature(
            feature_id=f"feature-{index:03d}",
            hunger_item_id=f"H-{index:03d}",
            phase_id=_PHASE_ID,
            title=f"Feature {index}",
            description="plan quickly",
            status="pending",
        )
        for index in range(feature_count)
    ]
    items = [
        HungerItem(
            id=feature.hunger_item_id,
            title=feature.title,
            priority=1.0,
            gap_score=1.0,
            status=HungerItemStatus.OPEN,
        )
        for feature in features
    ]
    mission = Mission(
        mission_id=_MISSION_ID,
        task_id=_TASK_ID,
        title="Mission Planner Perf",
        description="Perf fixture",
        phases=[
            MissionPhase(
                phase_id=_PHASE_ID,
                title="Phase",
                description="Phase",
                feature_ids=[feature.feature_id for feature in features],
                status="in_progress",
            )
        ],
        features=features,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        max_parallel_features=10,
    )
    repo.save_hunger_ledger(_TASK_ID, HungerLedger(task_id=_TASK_ID, items=items))
    repo.save_mission(mission)
    return repo, mission


def _measure_ms(func: Callable[[], object]) -> float:
    started = time.perf_counter()
    func()
    return (time.perf_counter() - started) * 1000.0


@pytest.mark.perf
def test_mission_planner_under_50ms_for_100_features() -> None:
    repo, mission = _repo_and_mission()
    planner = MissionPlanner(repo)
    budget = BudgetAllocation(phase=LoopPhase.EXPLORE, max_workers_per_loop=10)

    def _run() -> None:
        plan = planner.plan(_TASK_ID, 1, _snapshot(), budget, mission=mission)
        assert len(plan.assignments) == 10

    timings = [_measure_ms(_run) for _ in range(5)]
    median = statistics.median(timings)

    assert median <= _BUDGET_MS, (
        f"MissionPlanner median {median:.1f} ms exceeds {_BUDGET_MS:.1f} ms; "
        f"timings={[round(t, 1) for t in timings]}"
    )
