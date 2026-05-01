"""Unit tests for CommitManager (Task 8)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import ValidationReport
from hungerloop.services.commit_manager import CommitManager
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
