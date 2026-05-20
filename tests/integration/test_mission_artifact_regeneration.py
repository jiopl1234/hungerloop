from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
MISSION_ID = "mission-1"
PHASE_ID = "phase-1"
LOOP_ID = 1


class _FailingUpdater:
    def regenerate(self, task_id: str, *, best_workspace_root: Path) -> None:
        raise OSError(f"cannot regenerate {task_id} in {best_workspace_root}")


def _repo(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task(TASK_ID, "Build mission artifacts")
    return repo


def _mission_graph(repo: SQLiteRepository) -> Mission:
    feature = MissionFeature(
        feature_id="feature-1",
        hunger_item_id="H-001",
        phase_id=PHASE_ID,
        title="SQLite projected feature",
        description="Feature from repository state",
        status="done",
    )
    phase = MissionPhase(
        phase_id=PHASE_ID,
        title="Implementation",
        description="Implement the first feature",
        feature_ids=[feature.feature_id],
        validation_contract_ids=["ASSERT-1"],
        status="validating",
    )
    mission = Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Artifact Mission",
        description="Mission description",
        phases=[phase],
        features=[feature],
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    repo.save_mission(mission)
    repo.save_mission_phase(phase)
    repo.save_mission_feature(feature)
    repo.save_validation_contract(
        ValidationContract(
            mission_id=MISSION_ID,
            assertions=[
                ValidationAssertion(
                    assertion_id="ASSERT-1",
                    phase_id=PHASE_ID,
                    title="Artifact assertion",
                    description="Artifact assertion",
                    check_type="file_contains_regex",
                    params={"file": "report.md", "pattern": "ok"},
                    status="passed",
                    validated_at_loop=LOOP_ID,
                    evidence_ids=["EV-ASSERT-1"],
                )
            ],
        )
    )
    return mission


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-1",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=["EV-CANDIDATE"],
    )


def _report() -> ValidationReport:
    return ValidationReport(
        id="VAL-task-1-1",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate_state_id="CAND-task-1-1",
        baseline_state_id=None,
        verdict=ValidationVerdict.PASS,
        newly_passed_check_keys=["H-001:0"],
        currently_passed_check_keys=["H-001:0"],
        evidence_ids=["EV-VALIDATION"],
        has_real_progress=True,
    )


def _seed_candidate(
    repo: SQLiteRepository,
    workspace_manager: WorkspaceManager,
    *,
    features_yaml: str = "features:\n  - feature_id: worker-draft\n",
) -> CandidateState:
    candidate = _candidate()
    candidate_root = workspace_manager.create_candidate_workspace(TASK_ID, LOOP_ID)
    (candidate_root / "report.md").write_text("ok", encoding="utf-8")
    (candidate_root / "features.yaml").write_text(features_yaml, encoding="utf-8")
    repo.save_candidate(candidate)
    return candidate


def test_commit_tail_regenerates_all_mission_artifacts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mission = _mission_graph(repo)
    workspace_manager = WorkspaceManager(tmp_path / "workspace")
    candidate_draft = "features:\n  - feature_id: worker-draft\n    title: draft\n"
    candidate = _seed_candidate(
        repo,
        workspace_manager,
        features_yaml=candidate_draft,
    )

    decision = CommitManager(repo, workspace_manager).apply(candidate, _report())

    assert decision["committed"] is True
    best = workspace_manager.best_files_dir(TASK_ID)
    for artifact_name in [
        "mission.md",
        "features.yaml",
        "validation-contract.yaml",
        "services.yaml",
    ]:
        assert (best / artifact_name).exists()
    assert (best / "features.yaml").read_text(encoding="utf-8") != candidate_draft
    assert yaml.safe_load((best / "features.yaml").read_text(encoding="utf-8")) == {
        "features": [feature.model_dump(mode="json") for feature in mission.features]
    }
    assert repo.get_best_state(TASK_ID) is not None
    assert repo.candidate_status(candidate.id) == "committed"
    assert repo.list_events(TASK_ID, event_types=["mission.state_regenerated"])


def test_regeneration_failure_rolls_back_sqlite_and_rejects_candidate(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _mission_graph(repo)
    workspace_manager = WorkspaceManager(tmp_path / "workspace")
    candidate = _seed_candidate(repo, workspace_manager)

    decision = CommitManager(
        repo,
        workspace_manager,
        mission_state_updater=_FailingUpdater(),
    ).apply(candidate, _report())

    assert decision["committed"] is False
    assert decision["verdict"] is ValidationVerdict.FAIL
    assert decision["reason"] == "mission_state_regeneration_failed"
    assert repo.get_best_state(TASK_ID) is None
    assert repo.iter_accepted_checks(TASK_ID) == []
    assert repo.candidate_status(candidate.id) == "rejected"
    rejected = workspace_manager.rejected_files_dir(TASK_ID, LOOP_ID)
    assert (rejected / "features.yaml").exists()
    events = repo.list_events(
        TASK_ID,
        event_types=["MISSION_STATE_REGENERATION_FAILED"],
    )
    assert len(events) == 1
    assert events[0]["payload"]["candidate_state_id"] == candidate.id


def test_legacy_commit_does_not_write_mission_artifacts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workspace_manager = WorkspaceManager(tmp_path / "workspace")
    candidate = _seed_candidate(repo, workspace_manager)

    decision = CommitManager(repo, workspace_manager).apply(candidate, _report())

    assert decision["committed"] is True
    best = workspace_manager.best_files_dir(TASK_ID)
    assert (best / "report.md").exists()
    for artifact_name in [
        "mission.md",
        "validation-contract.yaml",
        "services.yaml",
    ]:
        assert not (best / artifact_name).exists()
