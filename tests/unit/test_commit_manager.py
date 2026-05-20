"""Unit tests for CommitManager (Task 8)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
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
