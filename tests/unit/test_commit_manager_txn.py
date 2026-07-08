"""Tests for transaction-aware CommitManager behavior (VAL-REF-007, VAL-REF-008, VAL-REF-020).

These tests verify:
- CommitManager remains strict I-3 when no transaction is open (VAL-REF-007)
- CommitManager tolerates only declared regressions for matching open same-task
  transactions (VAL-REF-008)
- Closed, rolled-back, wrong-task transactions do not relax I-3 (VAL-REF-008)
- Static checks catch score-derived commit-selection behavior (VAL-REF-006)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.refactor import RefactorTransaction, RefactorTransactionStatus
from hungerloop.models.validation import ValidationReport
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.workspace_manager import WorkspaceManager

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

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
    task_id: str = "t1",
) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-{task_id}-1",
        task_id=task_id,
        loop_id=1,
        candidate_state_id=f"CAND-{task_id}-1",
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed or [],
        regressed_check_keys=regressed or [],
        missing_evidence=missing_evidence or [],
        currently_passed_check_keys=currently_passed or [],
        evidence_ids=["ev-1"],
        has_real_progress=bool(newly_passed),
    )


def _best_state(task_id: str = "t1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="BEST-1",
        summary="baseline",
        score=0.0,
        accepted_check_keys=["H-001:0", "H-002:0"],
        workspace_ref="best",
    )


def _open_txn(
    task_id: str = "t1",
    declared_keys: list[str] | None = None,
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id="txn-001",
        task_id=task_id,
        opening_loop=1,
        deadline_loop=4,
        declared_regression_keys=declared_keys or ["H-001:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_best_state(task_id),
        snapshot_path=".txn_txn-001",
        status=RefactorTransactionStatus.OPEN,
    )


def _closed_txn(
    task_id: str = "t1",
    declared_keys: list[str] | None = None,
) -> RefactorTransaction:
    txn = _open_txn(task_id, declared_keys)
    return txn.model_copy(update={
        "status": RefactorTransactionStatus.CLOSED_SUCCESS,
        "closed_loop": 3,
        "close_reason": "all recovered",
    })


def _rolled_back_txn(
    task_id: str = "t1",
    declared_keys: list[str] | None = None,
) -> RefactorTransaction:
    txn = _open_txn(task_id, declared_keys)
    return txn.model_copy(update={
        "status": RefactorTransactionStatus.ROLLED_BACK,
        "closed_loop": 3,
        "close_reason": "unrecovered regressions",
    })


# -----------------------------------------------------------------------
# VAL-REF-007: Strict I-3 when no transaction is open
# -----------------------------------------------------------------------

class TestStrictI3NoTransaction:
    """Without an open transaction, CommitManager behaves exactly as v0.6."""

    def test_no_transaction_new_checks_commits(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(
            _candidate(),
            _report(newly_passed=["H-001:0"], currently_passed=["H-001:0"]),
        )
        assert result["committed"] is True

    def test_no_transaction_regressed_rejects(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-001:1"],
                regressed=["H-001:0"],
            ),
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"

    def test_no_transaction_no_new_checks_rejects(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(
            _candidate(),
            _report(verdict=ValidationVerdict.PASS, newly_passed=[]),
        )
        assert result["committed"] is False
        assert result["reason"] == "no_new_check_progress"

    def test_no_transaction_missing_evidence_rejects(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-001:0"],
                missing_evidence=["no evidence"],
            ),
        )
        assert result["committed"] is False
        assert result["reason"] == "missing_evidence"

    def test_no_transaction_fail_verdict_rejects(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(_candidate(), _report(verdict=ValidationVerdict.FAIL))
        assert result["committed"] is False
        assert result["reason"] == "verdict_fail"

    def test_none_transaction_preserves_strict_behavior(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        """Passing open_transaction=None should behave identically to no transaction."""
        ws.create_candidate_workspace("t1", 1)
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-001:1"],
                regressed=["H-001:0"],
            ),
            open_transaction=None,
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"


# -----------------------------------------------------------------------
# VAL-REF-008: Transaction-aware tolerance
# -----------------------------------------------------------------------

class TestTransactionTolerance:
    """With an open matching same-task transaction, declared regressions are tolerated."""

    def test_open_txn_tolerates_declared_regression(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0"],
                currently_passed=["H-002:0", "H-003:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is True

    def test_open_txn_rejects_undeclared_regression(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0", "H-002:0"],
                currently_passed=["H-003:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"

    def test_open_txn_rejects_no_new_progress(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                verdict=ValidationVerdict.PASS,
                newly_passed=[],
                regressed=["H-001:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "no_new_check_progress"

    def test_open_txn_rejects_missing_evidence(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0"],
                missing_evidence=["no evidence"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "missing_evidence"

    def test_open_txn_tolerates_multiple_declared_regressions(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0", "H-002:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0", "H-004:0"],
                regressed=["H-001:0", "H-002:0"],
                currently_passed=["H-003:0", "H-004:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is True

    def test_open_txn_mixed_declared_undeclared_rejects(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0", "H-002:0"],
                currently_passed=["H-003:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False


# -----------------------------------------------------------------------
# VAL-REF-008: Non-open or wrong-task transactions do not relax I-3
# -----------------------------------------------------------------------

class TestNonMatchingTransactions:
    """Closed, rolled-back, wrong-task, stale transactions provide no tolerance."""

    def test_closed_txn_does_not_relax_i3(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _closed_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"

    def test_rolled_back_txn_does_not_relax_i3(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _rolled_back_txn(task_id="t1", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0"],
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"

    def test_wrong_task_txn_does_not_relax_i3(
        self, cm: CommitManager, ws: WorkspaceManager
    ) -> None:
        ws.create_candidate_workspace("t1", 1)
        txn = _open_txn(task_id="t2", declared_keys=["H-001:0"])
        result = cm.apply(
            _candidate(task_id="t1"),
            _report(
                newly_passed=["H-003:0"],
                regressed=["H-001:0"],
                task_id="t1",
            ),
            open_transaction=txn,
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"


# -----------------------------------------------------------------------
# VAL-REF-006: Static checks for score-derived behavior
# -----------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT_SELECTION_PATH = REPO_ROOT / "src" / "hungerloop" / "services" / "commit_selection.py"
COMMIT_MANAGER_PATH = REPO_ROOT / "src" / "hungerloop" / "services" / "commit_manager.py"

_FORBIDDEN_SCORE_ATTRS = {"score", "proposed_score", "score_before", "score_after", "score_delta"}


def _score_references_in_commit_selection() -> list[str]:
    """Check commit_selection.py for score attribute references."""
    if not COMMIT_SELECTION_PATH.exists():
        return ["commit_selection.py does not exist"]
    tree = ast.parse(COMMIT_SELECTION_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SCORE_ATTRS:
            offenders.append(f"commit_selection.py:{node.lineno}:{node.attr}")
    return offenders


def _score_references_in_commit_gate() -> list[str]:
    """Check commit_manager.py gate/selection logic for score references.

    We scan _can_commit and _can_commit_with_transaction (the gate methods)
    but allow ``score=0.0`` in BestState construction (I-3 schema-only use).
    """
    tree = ast.parse(COMMIT_MANAGER_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        # Look for attribute access like .score, .proposed_score etc.
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SCORE_ATTRS:
            # Allow score=0.0 in BestState constructor kwargs (keyword assignment)
            # by checking if this is part of a keyword arg with value 0.0
            offenders.append(f"commit_manager.py:{node.lineno}:{node.attr}")
    # Filter: allow score=0.0 as a keyword argument in BestState construction
    filtered: list[str] = []
    for offender in offenders:
        # The existing BestState(score=0.0) is allowed per I-3 schema-only rule
        # We check if the line context shows it's a keyword arg assignment to 0.0
        parts = offender.split(":")
        lineno = int(parts[1])
        lines = COMMIT_MANAGER_PATH.read_text(encoding="utf-8").splitlines()
        if lineno <= len(lines):
            line = lines[lineno - 1]
            # Allow score=0.0 in BestState construction
            if "score=0.0" in line or "score=0." in line:
                continue
        filtered.append(offender)
    return filtered


class TestStaticScoreCheck:
    def test_commit_selection_has_no_score_references(self) -> None:
        offenders = _score_references_in_commit_selection()
        assert offenders == [], f"Score references found in commit_selection.py: {offenders}"

    def test_commit_gate_has_no_score_derived_logic(self) -> None:
        offenders = _score_references_in_commit_gate()
        assert offenders == [], f"Score-derived logic in commit_manager.py gate: {offenders}"

    def test_score_field_in_candidate_eval_does_not_affect_selection(self) -> None:
        """Demonstrate that score in evals does not change selection order."""
        from hungerloop.services.commit_selection import select_commit_candidate

        evals_low_score_first = [
            {"candidate_id": "c1", "newly_passed_check_keys": ["H-001:0"],
             "failing_check_keys": [], "regressed_check_keys": [],
             "missing_evidence": [], "verdict": ValidationVerdict.PASS, "score": 0.0},
            {"candidate_id": "c2", "newly_passed_check_keys": ["H-001:0"],
             "failing_check_keys": [], "regressed_check_keys": [],
             "missing_evidence": [], "verdict": ValidationVerdict.PASS, "score": 999.0},
        ]
        evals_high_score_first = [
            {"candidate_id": "c1", "newly_passed_check_keys": ["H-001:0"],
             "failing_check_keys": [], "regressed_check_keys": [],
             "missing_evidence": [], "verdict": ValidationVerdict.PASS, "score": 999.0},
            {"candidate_id": "c2", "newly_passed_check_keys": ["H-001:0"],
             "failing_check_keys": [], "regressed_check_keys": [],
             "missing_evidence": [], "verdict": ValidationVerdict.PASS, "score": 0.0},
        ]
        # Both should select c1 (lexicographic), regardless of score values
        r1 = select_commit_candidate(evals_low_score_first)
        r2 = select_commit_candidate(evals_high_score_first)
        assert r1 is not None
        assert r2 is not None
        assert r1["candidate_id"] == r2["candidate_id"] == "c1"
