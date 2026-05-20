from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.mission_state_updater import MissionStateUpdater


def _seed_mission(repo: InMemoryRepository) -> Mission:
    repo.create_task("task-1", "Build mission")
    feature = MissionFeature(
        feature_id="F-1",
        hunger_item_id="H-001",
        phase_id="phase-1",
        title="SQLite projected feature",
        description="Feature from repository state",
        status="in_progress",
    )
    phase = MissionPhase(
        phase_id="phase-1",
        title="Phase 1",
        description="Phase description",
        feature_ids=[feature.feature_id],
    )
    mission = Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission",
        description="Mission description",
        phases=[phase],
        features=[feature],
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    repo.save_mission(mission)
    repo.save_validation_contract(ValidationContract(mission_id=mission.mission_id))
    return mission


def test_candidate_draft_overwritten(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    mission = _seed_mission(repo)
    best_root = tmp_path / "workspace" / "tasks" / "task-1" / "best" / "files"
    candidate_root = (
        tmp_path
        / "workspace"
        / "tasks"
        / "task-1"
        / "candidates"
        / "loop_001"
        / "files"
    )
    best_root.mkdir(parents=True)
    candidate_root.mkdir(parents=True)
    candidate_draft = (
        "features:\n"
        "  - feature_id: worker-draft\n"
        "    title: should not survive promotion\n"
    )
    best_features = best_root / "features.yaml"
    best_features.write_text(candidate_draft, encoding="utf-8")
    (candidate_root / "features.yaml").write_text(candidate_draft, encoding="utf-8")

    MissionStateUpdater(repo).regenerate("task-1", best_workspace_root=best_root)

    assert best_features.read_text(encoding="utf-8") != candidate_draft
    parsed = yaml.safe_load(best_features.read_text(encoding="utf-8"))
    assert parsed == {
        "features": [feature.model_dump(mode="json") for feature in mission.features]
    }
