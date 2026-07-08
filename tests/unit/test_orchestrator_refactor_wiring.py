"""Tests for orchestrator refactor transaction wiring (VAL-REF-017, VAL-REF-018, VAL-REF-022).

Covers:
- Orchestrator passes open transactions to CommitManager (VAL-REF-017)
- Orchestrator closes due transactions at deadline or forced close (VAL-REF-018)
- Disabled policy ignores stale transaction state (VAL-REF-022)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.refactor import RefactorTransaction, RefactorTransactionStatus
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.refactor_transaction_manager import (
    RefactorTransactionManager,
)
from hungerloop.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_best_state(task_id: str = "task-1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="BEST-001",
        summary="baseline",
        accepted_check_keys=["H-001:0", "H-002:0"],
    )


def _make_open_txn(
    task_id: str = "task-1",
    declared_keys: list[str] | None = None,
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id="txn-001",
        task_id=task_id,
        opening_loop=5,
        deadline_loop=8,
        declared_regression_keys=declared_keys or ["H-001:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=".txn_txn-001",
        status=RefactorTransactionStatus.OPEN,
    )


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def txn_manager(repo: InMemoryRepository, ws: WorkspaceManager) -> RefactorTransactionManager:
    return RefactorTransactionManager(repo=repo, workspace_manager=ws)


# ---------------------------------------------------------------------------
# VAL-REF-017: Orchestrator passes open transactions into commit gating
# ---------------------------------------------------------------------------


class TestOrchestratorPassesTransaction:
    """The orchestrator retrieves and passes open transactions to commit gating."""

    def test_get_active_transaction_returns_open_when_enabled(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """When policy is enabled and a transaction is open, get_active_transaction returns it."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=True))
        best = _make_best_state()
        repo.save_best_state(best)

        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        active = txn_manager.get_active_transaction("task-1")
        assert active is not None
        assert active.transaction_id == "txn-001"
        assert active.status == RefactorTransactionStatus.OPEN

    def test_get_active_transaction_returns_none_when_disabled(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """When policy is disabled, get_active_transaction returns None even with stale row."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=False))

        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        active = txn_manager.get_active_transaction("task-1")
        assert active is None

    def test_get_active_transaction_returns_none_for_wrong_task(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """When the transaction is for a different task, returns None."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=True))

        txn = _make_open_txn(task_id="task-2")
        repo.save_refactor_transaction(txn)

        active = txn_manager.get_active_transaction("task-1")
        assert active is None


# ---------------------------------------------------------------------------
# VAL-REF-018: Orchestrator closes due transactions
# ---------------------------------------------------------------------------


class TestOrchestratorClosesDueTransactions:
    """The orchestrator invokes transaction closure when deadline is due."""

    def test_settle_if_due_at_deadline(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """settle_if_due closes a transaction when the deadline is reached."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy(
            "task-1",
            HungerPolicy(refactor_transactions_enabled=True, refactor_deadline_loops=3),
        )
        best = _make_best_state()
        repo.save_best_state(best)
        ws.ensure_task_workspace("task-1")
        (ws.best_files_dir("task-1") / "file.py").write_text("content")

        # Open a transaction at loop 5, deadline = 8
        open_result = txn_manager.open(
            task_id="task-1",
            loop_id=5,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # At loop 7, not yet due
        result = txn_manager.settle_if_due(task_id="task-1", current_loop=7)
        assert result is None

        # At loop 8, due -> settle
        result = txn_manager.settle_if_due(task_id="task-1", current_loop=8)
        assert result is not None
        # Without strict superset, should roll back
        assert result.status == RefactorTransactionStatus.ROLLED_BACK

    def test_settle_if_due_no_open_transaction(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """settle_if_due returns None when no open transaction exists."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=True))

        result = txn_manager.settle_if_due(task_id="task-1", current_loop=100)
        assert result is None

    def test_settle_if_due_disabled_policy(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        txn_manager: RefactorTransactionManager,
    ) -> None:
        """settle_if_due returns None when policy is disabled."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=False))

        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        result = txn_manager.settle_if_due(task_id="task-1", current_loop=100)
        assert result is None

        # Transaction should remain open
        assert repo.get_open_refactor_transaction("task-1") is not None


# ---------------------------------------------------------------------------
# VAL-REF-022: Disabled policy preserves strict v0.6 behavior
# ---------------------------------------------------------------------------


class TestDisabledPolicyOrchestrator:
    """Disabled policy preserves strict v0.6 behavior with stale rows."""

    def test_disabled_policy_commit_manager_no_tolerance(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        """CommitManager with disabled policy does not tolerate regressions,
        even with a stale open transaction."""
        repo.create_task("task-1", "test")
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=False))
        best = _make_best_state()
        repo.save_best_state(best)

        # Stale open transaction
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        # CommitManager should not tolerate regressions
        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = CandidateState(
            id="CAND-1",
            task_id="task-1",
            loop_id=1,
            summary="test",
            workspace_ref="candidates/loop_001",
        )
        report = ValidationReport(
            id="VAL-1",
            task_id="task-1",
            loop_id=1,
            candidate_state_id="CAND-1",
            baseline_state_id=None,
            verdict=ValidationVerdict.PASS,
            newly_passed_check_keys=["H-003:0"],
            regressed_check_keys=["H-001:0"],
            currently_passed_check_keys=["H-002:0", "H-003:0"],
            evidence_ids=["ev-1"],
            has_real_progress=True,
        )

        # Mock repo methods needed by commit
        repo.get_mission = MagicMock(return_value=None)
        repo.list_events = MagicMock(return_value=[])

        result = cm.apply(
            candidate,
            report,
            open_transaction=txn,  # Even passing it explicitly shouldn't help
        )
        assert result["committed"] is False
        assert result["reason"] == "regressed_checks_detected"
