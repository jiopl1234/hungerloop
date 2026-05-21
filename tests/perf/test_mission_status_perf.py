"""Perf budget for ``hungerloop mission status`` (PRD §9 NFR)."""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.repository.sqlite_repo import SQLiteRepository

_DEFAULT_BUDGET_MS = 200.0
_TASK_ID = "perf-mission-1"
_MISSION_ID = "mission-perf-mission-1"
_PHASE_ID = "phase-1"


def _budget_ms() -> float:
    raw = os.environ.get("HUNGERLOOP_PERF_BUDGET_MS")
    if not raw:
        return _DEFAULT_BUDGET_MS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_MS


@pytest.fixture
def loaded_context(tmp_path: Path) -> CliContext:
    """Seed a mission with 100 features in a real SQLite repository."""
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task(_TASK_ID, "mission status perf")
    features = [
        MissionFeature(
            feature_id=f"feat-{index:03d}",
            hunger_item_id=f"H-{index:03d}",
            phase_id=_PHASE_ID,
            title=f"Feature {index}",
            description="perf",
            status="pending",
        )
        for index in range(100)
    ]
    mission = Mission(
        mission_id=_MISSION_ID,
        task_id=_TASK_ID,
        title="Mission status perf",
        description="Mission status perf",
        phases=[
            MissionPhase(
                phase_id=_PHASE_ID,
                title="Active phase",
                description="Active",
                feature_ids=[feature.feature_id for feature in features],
                status="in_progress",
            )
        ],
        features=features,
        created_at=datetime.now(timezone.utc),
    )
    repo.save_mission(mission)
    return CliContext(repo=repo, workspace_root=tmp_path)


def _measure_ms(invoke: Callable[[], object]) -> float:
    start = time.perf_counter()
    invoke()
    return (time.perf_counter() - start) * 1000.0


@pytest.mark.perf
def test_mission_status_under_200ms(loaded_context: CliContext) -> None:
    runner = CliRunner()

    def _run() -> None:
        result = runner.invoke(
            cli, ["mission", "status", _TASK_ID], obj=loaded_context
        )
        assert result.exit_code == 0, result.output

    timings = [_measure_ms(_run) for _ in range(5)]
    median = statistics.median(timings)
    budget = _budget_ms()
    assert median < budget, (
        f"hungerloop mission status median {median:.1f} ms exceeds "
        f"{budget:.1f} ms; timings={[round(t, 1) for t in timings]}"
    )
