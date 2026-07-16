"""Unit tests for CommitManager (Task 8)."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.validation import ValidationReport
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.validation_pipeline import ValidationPipelineResult
from hungerloop.services.workspace_manager import WorkspaceManager


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def repo() -> MagicMock:
    r = MagicMock()
    r.get_mission.return_value = None
    r.list_events.return_value = []
    return r


@pytest.fixture
def cm(repo: MagicMock, ws: WorkspaceManager) -> CommitManager:
    return CommitManager(repo=repo, workspace_manager=ws)


def _candidate(task_id: str = "t1", loop_id: int = 1) -> CandidateState:
    return CandidateState(
        id=f"CAND-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        summary="test candidate",
        workspace_ref=f"candidates/loop_{loop_id:03d}",
    )


def _report(
    verdict: ValidationVerdict = ValidationVerdict.PASS,
    newly_passed: list[str] | None = None,
    regressed: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    currently_passed: list[str] | None = None,
) -> ValidationReport:
    return ValidationReport(
        id="VAL-t1-1",
        task_id="t1",
        loop_id=1,
        candidate_state_id="CAND-t1-1",
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed or [],
        regressed_check_keys=regressed or [],
        missing_evidence=missing_evidence or [],
        currently_passed_check_keys=currently_passed or [],
        evidence_ids=["ev-1"],
        has_real_progress=bool(newly_passed),
    )


def test_pass_with_new_checks_commits(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=["H-001:0"],
        currently_passed=["H-001:0"],
    )
    result = cm.apply(candidate, report)
    assert result["committed"] is True


def test_partial_with_new_checks_commits(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        verdict=ValidationVerdict.PARTIAL,
        newly_passed=["H-001:0"],
        currently_passed=["H-001:0"],
    )
    result = cm.apply(candidate, report)
    assert result["committed"] is True


def test_pipeline_result_commit_gate_uses_deterministic_report(
    cm: CommitManager,
    ws: WorkspaceManager,
) -> None:
    ws.create_candidate_workspace("t1", 1)
    deterministic_report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=["H-001:0"],
        currently_passed=["H-001:0"],
    )
    scrutiny_report = _report(verdict=ValidationVerdict.FAIL)
    pipeline_result = ValidationPipelineResult(
        deterministic_report=deterministic_report,
        scrutiny_report=scrutiny_report,
        user_testing_report=None,
        pipeline_verdict="fail",
        stages_run=["deterministic", "scrutiny"],
    )

    result = cm.apply(_candidate(), pipeline_result)

    assert result["committed"] is True


def test_no_newly_passed_rejects(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(verdict=ValidationVerdict.PASS, newly_passed=[])
    result = cm.apply(candidate, report)
    assert result["committed"] is False
    assert result["reason"] == "no_new_check_progress"


def test_regressed_checks_rejects(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=["H-001:1"],
        regressed=["H-001:0"],
    )
    result = cm.apply(candidate, report)
    assert result["committed"] is False
    assert result["reason"] == "regressed_checks_detected"


def test_missing_evidence_rejects(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=["H-001:0"],
        missing_evidence=["No evidence"],
    )
    result = cm.apply(candidate, report)
    assert result["committed"] is False
    assert result["reason"] == "missing_evidence"


def test_fail_verdict_rejects(cm: CommitManager, ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(verdict=ValidationVerdict.FAIL)
    result = cm.apply(candidate, report)
    assert result["committed"] is False
    assert result["reason"] == "verdict_fail"


def test_commit_promotes_workspace(cm: CommitManager, ws: WorkspaceManager) -> None:
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "output.txt").write_text("result")

    candidate = _candidate()
    report = _report(
        newly_passed=["H-001:0"],
        currently_passed=["H-001:0"],
    )
    cm.apply(candidate, report)

    best = ws.best_files_dir("t1")
    assert (best / "output.txt").read_text() == "result"


def test_reject_moves_to_rejected(cm: CommitManager, ws: WorkspaceManager) -> None:
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "broken.txt").write_text("bad")

    candidate = _candidate()
    report = _report(verdict=ValidationVerdict.FAIL)
    cm.apply(candidate, report)

    rejected = ws.rejected_files_dir("t1", 1)
    assert (rejected / "broken.txt").read_text() == "bad"
    assert not candidate_dir.exists()


def test_commit_persists_best_state_with_correct_fields(
    cm: CommitManager, repo: MagicMock, ws: WorkspaceManager
) -> None:
    """Verify that commit persists a BestState with the right fields."""
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        newly_passed=["H-001:0"],
        currently_passed=["H-001:0", "H-002:0"],
    )
    cm.apply(candidate, report)

    repo.save_best_state.assert_called_once()
    best = repo.save_best_state.call_args.args[0]
    assert best.task_id == "t1"
    assert best.state_id == "CAND-t1-1"
    assert best.validation_id == "VAL-t1-1"
    assert best.accepted_check_keys == ["H-001:0", "H-002:0"]
    assert best.workspace_ref == "best"
    assert best.score == 0.0
    repo.mark_candidate_committed.assert_called_once_with("CAND-t1-1")


def test_no_score_based_commit(cm: CommitManager, ws: WorkspaceManager) -> None:
    """Score must never drive promotion; only newly-passed checks may."""
    ws.create_candidate_workspace("t1", 1)
    candidate = _candidate()
    report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=[],
        currently_passed=["H-001:0"],
    )

    result = cm.apply(candidate, report)

    assert result["committed"] is False
    assert result["reason"] == "no_new_check_progress"


def test_reject_records_failure_and_marks_candidate(
    cm: CommitManager, repo: MagicMock, ws: WorkspaceManager
) -> None:
    """Verify that reject calls the right repo methods and doesn't save BestState."""
    ws.create_candidate_workspace("t1", 1)
    cm.apply(_candidate(), _report(verdict=ValidationVerdict.FAIL))
    repo.mark_candidate_rejected.assert_called_once_with("CAND-t1-1")
    repo.add_failure_from_validation.assert_called_once()
    repo.save_best_state.assert_not_called()


def test_reject_reason_priority_verdict_fail_over_others(
    cm: CommitManager, ws: WorkspaceManager
) -> None:
    """When multiple reject conditions hold, verdict_fail takes priority."""
    ws.create_candidate_workspace("t1", 1)
    report = _report(
        verdict=ValidationVerdict.FAIL,
        newly_passed=[],
        regressed=["H-001:0"],
        missing_evidence=["missing log"],
    )
    result = cm.apply(_candidate(), report)
    assert result["reason"] == "verdict_fail"


def test_reject_reason_priority_regressed_over_missing_evidence(
    cm: CommitManager, ws: WorkspaceManager
) -> None:
    """When both regressed and missing_evidence hold, regressed takes priority."""
    ws.create_candidate_workspace("t1", 1)
    report = _report(
        verdict=ValidationVerdict.PASS,
        newly_passed=["H-001:1"],
        regressed=["H-001:0"],
        missing_evidence=["missing log"],
    )
    result = cm.apply(_candidate(), report)
    assert result["reason"] == "regressed_checks_detected"


class _RecordingUpdater:
    def __init__(self, order: list[str] | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.order = order

    def regenerate(self, task_id: str, *, best_workspace_root: Path) -> None:
        if self.order is not None:
            self.order.append("regenerate")
        self.calls.append((task_id, best_workspace_root))


class _MutatingUpdater:
    """Updater that rewrites a mission artifact inside best/files."""

    def regenerate(self, task_id: str, *, best_workspace_root: Path) -> None:
        (best_workspace_root / "mission.md").write_text(
            "# regenerated after commit", encoding="utf-8"
        )


class _FailingUpdater:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[tuple[str, Path]] = []

    def regenerate(self, task_id: str, *, best_workspace_root: Path) -> None:
        self.calls.append((task_id, best_workspace_root))
        raise self.exc


class _RecordingTransaction:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def __enter__(self) -> None:
        self.order.append("transaction_enter")

    def __exit__(self, *args: Any) -> None:
        self.order.append("transaction_exit")


def test_mission_commit_regenerates_after_repository_writes(
    repo: MagicMock,
    ws: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    repo.get_mission.return_value = object()
    repo.transaction.return_value = _RecordingTransaction(order)
    repo.save_best_state.side_effect = lambda _best: order.append("save_best_state")
    repo.mark_candidate_committed.side_effect = lambda _candidate_id: order.append(
        "mark_candidate_committed"
    )
    repo.save_accepted_check.side_effect = lambda **_kwargs: order.append(
        "save_accepted_check"
    )
    updater = _RecordingUpdater(order)
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "output.txt").write_text("result", encoding="utf-8")
    real_promote = ws.promote_candidate_to_best

    def recording_promote(*, task_id: str, loop_id: int) -> None:
        order.append("promote")
        real_promote(task_id=task_id, loop_id=loop_id)

    monkeypatch.setattr(ws, "promote_candidate_to_best", recording_promote)

    cm_with_updater = CommitManager(
        repo=repo,
        workspace_manager=ws,
        mission_state_updater=updater,
    )
    result = cm_with_updater.apply(
        _candidate(),
        _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
    )

    assert result["committed"] is True
    assert order == [
        "transaction_enter",
        "promote",
        "save_best_state",
        "mark_candidate_committed",
        "save_accepted_check",
        "regenerate",
        "transaction_exit",
    ]
    assert updater.calls == [("t1", ws.best_files_dir("t1"))]


def test_mission_commit_refreshes_best_manifest_after_regeneration(
    repo: MagicMock,
    ws: WorkspaceManager,
) -> None:
    repo.get_mission.return_value = object()
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "output.txt").write_text("result", encoding="utf-8")

    cm_with_updater = CommitManager(
        repo=repo,
        workspace_manager=ws,
        mission_state_updater=_MutatingUpdater(),
    )
    result = cm_with_updater.apply(
        _candidate(),
        _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
    )

    assert result["committed"] is True
    # Regeneration mutated best/files after promote wrote the manifest;
    # the manifest must reflect the live tree or every identity check
    # (baseline validation, continuation dedup) reports a false mismatch.
    assert ws.workspace_matches_best_manifest("t1", ws.best_files_dir("t1"))


def test_mission_commit_marks_completed_features_before_regeneration(
    repo: MagicMock,
    ws: WorkspaceManager,
) -> None:
    order: list[str] = []
    repo.get_mission.return_value = object()
    repo.transaction.return_value = _RecordingTransaction(order)
    repo.update_feature_status.side_effect = lambda feature_id, status: order.append(
        f"feature:{feature_id}:{status}"
    )
    updater = _RecordingUpdater(order)
    ws.create_candidate_workspace("t1", 1)

    cm_with_updater = CommitManager(
        repo=repo,
        workspace_manager=ws,
        mission_state_updater=updater,
    )
    result = cm_with_updater.apply(
        _candidate(),
        _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
        completed_feature_ids=["F-1"],
    )

    assert result["committed"] is True
    assert order == [
        "transaction_enter",
        "feature:F-1:done",
        "regenerate",
        "transaction_exit",
    ]
    repo.update_feature_status.assert_called_once_with("F-1", "done")


def test_legacy_commit_skips_mission_state_regeneration(
    repo: MagicMock,
    ws: WorkspaceManager,
) -> None:
    repo.get_mission.return_value = None
    updater = _RecordingUpdater()
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "output.txt").write_text("result", encoding="utf-8")

    cm_with_updater = CommitManager(
        repo=repo,
        workspace_manager=ws,
        mission_state_updater=updater,
    )
    result = cm_with_updater.apply(
        _candidate(),
        _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
    )

    assert result["committed"] is True
    assert updater.calls == []
    best = ws.best_files_dir("t1")
    for artifact_name in [
        "mission.md",
        "features.yaml",
        "validation-contract.yaml",
        "services.yaml",
    ]:
        assert not (best / artifact_name).exists()


def test_regeneration_failure_rejects_candidate_and_returns_fail_verdict(
    repo: MagicMock,
    ws: WorkspaceManager,
) -> None:
    repo.get_mission.return_value = object()
    failure = RuntimeError("disk full")
    updater = _FailingUpdater(failure)
    candidate_dir = ws.create_candidate_workspace("t1", 1)
    (candidate_dir / "output.txt").write_text("result", encoding="utf-8")

    cm_with_updater = CommitManager(
        repo=repo,
        workspace_manager=ws,
        mission_state_updater=updater,
    )
    result = cm_with_updater.apply(
        _candidate(),
        _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
    )

    assert result == {
        "committed": False,
        "reason": "mission_state_regeneration_failed",
        "verdict": ValidationVerdict.FAIL,
    }
    assert updater.calls == [("t1", ws.best_files_dir("t1"))]
    assert not candidate_dir.exists()
    rejected = ws.rejected_files_dir("t1", 1)
    assert (rejected / "output.txt").read_text(encoding="utf-8") == "result"
    repo.mark_candidate_rejected.assert_called_once_with("CAND-t1-1")
    repo.append_event.assert_called_once()
    assert repo.append_event.call_args.args[0] is EventType.MISSION_STATE_REGENERATION_FAILED
