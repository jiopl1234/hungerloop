from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.mission_state_updater import MissionStateUpdater

_TASK_ID = "perf-regen"
_MISSION_ID = "mission-perf-regen"
_PHASE_ID = "phase-1"
_BUDGET_MS = 100.0


def _repo_with_mission(feature_count: int = 50) -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.create_task(_TASK_ID, "mission state updater perf")
    features = [
        MissionFeature(
            feature_id=f"feature-{index:03d}",
            hunger_item_id=f"H-{index:03d}",
            phase_id=_PHASE_ID,
            title=f"Feature {index}",
            description="regenerate mirrors quickly",
            status="pending",
        )
        for index in range(feature_count)
    ]
    assertions = [
        ValidationAssertion(
            assertion_id=f"ASSERT-{index:03d}",
            phase_id=_PHASE_ID,
            title=f"Assertion {index}",
            description="assertion",
            check_type="behavioral_assertion",
            params={"feature": feature.feature_id},
        )
        for index, feature in enumerate(features)
    ]
    repo.save_mission(
        Mission(
            mission_id=_MISSION_ID,
            task_id=_TASK_ID,
            title="Mission State Updater Perf",
            description="Perf fixture",
            phases=[
                MissionPhase(
                    phase_id=_PHASE_ID,
                    title="Phase",
                    description="Phase",
                    feature_ids=[feature.feature_id for feature in features],
                    validation_contract_ids=[
                        assertion.assertion_id for assertion in assertions
                    ],
                    status="in_progress",
                )
            ],
            features=features,
            created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        )
    )
    repo.save_validation_contract(
        ValidationContract(mission_id=_MISSION_ID, assertions=assertions)
    )
    return repo


def _measure_ms(func: Callable[[], object]) -> float:
    start = time.perf_counter()
    func()
    return (time.perf_counter() - start) * 1000.0


@pytest.mark.perf
def test_mission_state_updater_regenerate_under_100ms(tmp_path: Path) -> None:
    repo = _repo_with_mission()
    updater = MissionStateUpdater(repo)
    best_root = tmp_path / "best"
    best_root.mkdir()

    def _run() -> None:
        result = updater.regenerate(_TASK_ID, best_workspace_root=best_root)
        assert len(result.artifact_paths) == 4

    timings = [_measure_ms(_run) for _ in range(5)]
    median = statistics.median(timings)

    assert median <= _BUDGET_MS, (
        f"MissionStateUpdater median {median:.1f} ms exceeds {_BUDGET_MS:.1f} ms; "
        f"timings={[round(t, 1) for t in timings]}"
    )
