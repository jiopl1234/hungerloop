"""Tests for RefactorTransactionManager (VAL-REF-009 through VAL-REF-027).

Covers:
- Transaction opening: policy gate, single-open, declared key validation,
  limits, snapshot creation, deadline derivation (VAL-REF-009, VAL-REF-010,
  VAL-REF-026)
- Close success: strict superset, declared keys recovered (VAL-REF-011,
  VAL-REF-027)
- Close failure: rollback, exact restoration, retryable (VAL-REF-012,
  VAL-REF-013)
- Snapshot independence from later best-workspace changes (VAL-REF-010)
- Audit events stable and non-secret (VAL-REF-024)
- Close idempotency and cleanup safety (VAL-REF-023)
- Disabled policy prevents all behavior (VAL-REF-022)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.refactor import (
    RefactorTransactionStatus,
)
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.refactor_transaction_manager import (
    RefactorTransactionManager,
)
from hungerloop.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_task(
    repo: InMemoryRepository,
    task_id: str = "task-1",
    accepted_keys: list[str] | None = None,
    policy: HungerPolicy | None = None,
) -> BestState:
    """Create task, set policy, create best state, and write best files."""
    accepted_keys = accepted_keys or ["H-001:0", "H-002:0"]
    repo.create_task(task_id, "test goal")
    if policy is None:
        policy = HungerPolicy(refactor_transactions_enabled=True)
    repo.set_hunger_policy(task_id, policy)

    best = BestState(
        task_id=task_id,
        state_id="BEST-001",
        summary="baseline state",
        accepted_check_keys=accepted_keys,
        updated_at_loop=5,
    )
    repo.save_best_state(best)
    return best


def _write_best_files(ws: WorkspaceManager, task_id: str, files: dict[str, str]) -> None:
    """Write files into the best workspace for a task."""
    best_dir = ws.best_files_dir(task_id)
    best_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        full = best_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def manager(repo: InMemoryRepository, ws: WorkspaceManager) -> RefactorTransactionManager:
    return RefactorTransactionManager(repo=repo, workspace_manager=ws)


# ---------------------------------------------------------------------------
# VAL-REF-009: Transaction opening is policy-gated and single-open
# ---------------------------------------------------------------------------


class TestTransactionOpen:
    """Opening a refactor transaction validates all preconditions."""

    def test_disabled_policy_rejects_open(
        self, repo: InMemoryRepository, manager: RefactorTransactionManager
    ) -> None:
        _setup_task(repo, policy=HungerPolicy(refactor_transactions_enabled=False))
        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring module X",
        )
        assert result.success is False
        assert "disabled" in result.reason.lower()
        # No transaction should be persisted
        assert repo.get_open_refactor_transaction("task-1") is None

    def test_existing_open_transaction_rejects_second_open(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content1"})

        result1 = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="first refactor",
        )
        assert result1.success is True

        result2 = manager.open(
            task_id="task-1",
            loop_id=12,
            declared_regression_keys=["H-002:0"],
            rationale="second refactor",
        )
        assert result2.success is False
        assert "already" in result2.reason.lower() or "open" in result2.reason.lower()

    def test_declared_keys_must_be_currently_accepted(
        self,
        repo: InMemoryRepository,
        manager: RefactorTransactionManager,
        ws: WorkspaceManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content1"})

        # H-003:0 is not in accepted_check_keys
        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-003:0"],
            rationale="refactor with unknown key",
        )
        assert result.success is False
        assert "accepted" in result.reason.lower() or "declared" in result.reason.lower()

    def test_over_limit_declared_keys_rejected(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        policy = HungerPolicy(
            refactor_transactions_enabled=True,
            max_declared_regressions=2,
        )
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0", "H-003:0"], policy=policy)
        _write_best_files(ws, "task-1", {"file1.py": "content1"})

        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)
        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0", "H-002:0", "H-003:0"],
            rationale="too many declared keys",
        )
        assert result.success is False
        assert "limit" in result.reason.lower() or "max" in result.reason.lower()

    def test_successful_open_creates_snapshot(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content1", "dir/file2.py": "content2"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring module X",
        )
        assert result.success is True
        assert result.transaction is not None

        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.OPEN
        assert txn.declared_regression_keys == ["H-001:0"]
        assert txn.baseline_accepted_check_keys == ["H-001:0", "H-002:0"]
        assert txn.baseline_accepted_check_count == 2
        assert txn.opening_loop == 10

        # Snapshot directory should exist
        snapshot_dir = ws.task_root("task-1") / txn.snapshot_path
        assert snapshot_dir.exists()
        assert (snapshot_dir / "file1.py").read_text(encoding="utf-8") == "content1"
        assert (snapshot_dir / "dir" / "file2.py").read_text(encoding="utf-8") == "content2"

    def test_open_emits_audit_event(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content1"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring module X",
        )
        assert result.success is True

        events = repo.list_events("task-1")
        open_events = [e for e in events if e.get("event_type") == "refactor_txn_opened"]
        assert len(open_events) == 1
        payload = open_events[0]["payload"]
        assert isinstance(payload, dict)
        assert "transaction_id" in payload
        assert payload["task_id"] == "task-1"
        assert payload["loop_id"] == 10
        assert "deadline_loop" in payload
        assert payload["declared_regression_keys"] == ["H-001:0"]
        # No secrets in payload
        for key, val in payload.items():
            if isinstance(val, str):
                assert "key" not in key.lower() or key == "transaction_id"
                assert "secret" not in str(val).lower()
                assert "password" not in str(val).lower()


# ---------------------------------------------------------------------------
# VAL-REF-010: Snapshot independence and baseline state
# ---------------------------------------------------------------------------


class TestSnapshotIndependence:
    """Open snapshots are independent of later best-workspace changes."""

    def test_snapshot_unaffected_by_best_changes(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "original"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is True
        txn = result.transaction
        assert txn is not None

        snapshot_dir = ws.task_root("task-1") / txn.snapshot_path

        # Mutate best files after open
        best_dir = ws.best_files_dir("task-1")
        (best_dir / "file1.py").write_text("modified", encoding="utf-8")
        (best_dir / "new_file.py").write_text("new content", encoding="utf-8")

        # Snapshot should still have the original content
        assert (snapshot_dir / "file1.py").read_text(encoding="utf-8") == "original"
        assert not (snapshot_dir / "new_file.py").exists()

    def test_baseline_best_state_recorded(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is True
        txn = result.transaction
        assert txn is not None

        assert txn.baseline_best_state.task_id == "task-1"
        assert txn.baseline_best_state.accepted_check_keys == ["H-001:0", "H-002:0"]
        assert txn.baseline_accepted_check_count == 2


# ---------------------------------------------------------------------------
# VAL-REF-026: Deadlines are derived and cannot be extended
# ---------------------------------------------------------------------------


class TestDeadlineDerivation:
    """Deadline is derived from opening loop plus policy.refactor_deadline_loops."""

    def test_deadline_equals_opening_loop_plus_policy(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        policy = HungerPolicy(
            refactor_transactions_enabled=True,
            refactor_deadline_loops=5,
        )
        _setup_task(repo, policy=policy)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is True
        txn = result.transaction
        assert txn is not None
        assert txn.deadline_loop == 15  # 10 + 5

    def test_worker_cannot_supply_deadline(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        """Handoff payloads cannot supply or override the deadline.

        The manager.open method does not accept a deadline parameter;
        deadline is always derived from opening_loop + policy.
        """
        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is True
        txn = result.transaction
        assert txn is not None
        # Deadline is always opening_loop + policy.refactor_deadline_loops
        assert txn.deadline_loop == 10 + 3  # default refactor_deadline_loops=3


# ---------------------------------------------------------------------------
# VAL-REF-011: Close success requires strict superset
# ---------------------------------------------------------------------------


class TestCloseSuccess:
    """Closing succeeds only when declared keys pass and accepted checks are strict superset."""

    def test_close_success_with_strict_superset(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        txn = open_result.transaction
        assert txn is not None

        # Simulate that all declared keys recovered + net new progress
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-001:0", "H-002:0", "H-003:0"],  # strict superset
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(
            task_id="task-1",
            loop_id=12,
            force=True,
        )
        assert close_result.success is True
        assert close_result.status == RefactorTransactionStatus.CLOSED_SUCCESS

        # Verify transaction is updated
        updated = repo.get_refactor_transaction(txn.transaction_id)
        assert updated is not None
        assert updated.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert updated.closed_loop == 12

    def test_close_fails_without_strict_superset_same_count(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Same count, different keys - not a strict superset
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-001:0", "H-003:0"],  # H-002:0 missing, H-003:0 added
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(
            task_id="task-1",
            loop_id=12,
            force=True,
        )
        assert close_result.success is False
        # Should have rolled back
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK

    def test_close_fails_when_declared_key_not_recovered(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # H-001:0 still not in accepted checks (not recovered)
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-002:0", "H-003:0"],  # H-001:0 missing
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(
            task_id="task-1",
            loop_id=12,
            force=True,
        )
        assert close_result.success is False
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK


# ---------------------------------------------------------------------------
# VAL-REF-012, VAL-REF-013: Rollback restores exact baseline
# ---------------------------------------------------------------------------


class TestRollback:
    """Rollback restores exact baseline best files and best state."""

    def test_rollback_restores_files(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "original", "dir/file2.py": "original2"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        txn = open_result.transaction
        assert txn is not None

        # Mutate best files during transaction
        best_dir = ws.best_files_dir("task-1")
        (best_dir / "file1.py").write_text("modified", encoding="utf-8")
        (best_dir / "new_file.py").write_text("new", encoding="utf-8")
        (best_dir / "dir" / "file2.py").write_text("modified2", encoding="utf-8")

        # Close fails -> rollback
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed refactor",
            accepted_check_keys=["H-002:0"],  # not a superset
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert close_result.success is False
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK

        # Files should be restored
        assert (best_dir / "file1.py").read_text(encoding="utf-8") == "original"
        assert (best_dir / "dir" / "file2.py").read_text(encoding="utf-8") == "original2"
        # New file should be removed
        assert not (best_dir / "new_file.py").exists()

    def test_rollback_restores_best_state(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Mutate best state
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed refactor",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK

        # Best state should be restored to baseline
        restored = repo.get_best_state("task-1")
        assert restored is not None
        assert restored.accepted_check_keys == ["H-001:0", "H-002:0"]
        assert restored.state_id == "BEST-001"

    def test_rollback_emits_event(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK

        events = repo.list_events("task-1")
        rollback_events = [e for e in events if e.get("event_type") == "refactor_txn_rolled_back"]
        assert len(rollback_events) == 1
        payload = rollback_events[0]["payload"]
        assert isinstance(payload, dict)
        assert "transaction_id" in payload
        assert payload["task_id"] == "task-1"

    def test_rollback_retryable_on_restoration_failure(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        """If rollback restoration fails, status remains open/retryable."""
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        txn = open_result.transaction
        assert txn is not None

        # Make restore fail by monkey-patching the manager's internal method
        original_restore = manager._restore_best_from_snapshot
        manager._restore_best_from_snapshot = lambda t: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]

        # Mutate best state so close fails
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        # Rollback should have failed -> transaction stays open/retryable
        assert close_result.success is False

        # Restore original for cleanup
        manager._restore_best_from_snapshot = original_restore  # type: ignore[method-assign]

        # Transaction should still be open (not rolled_back)
        updated_txn = repo.get_refactor_transaction(txn.transaction_id)
        assert updated_txn is not None
        assert updated_txn.status == RefactorTransactionStatus.OPEN

        # No rolled_back event should be persisted
        events = repo.list_events("task-1")
        rollback_events = [e for e in events if e.get("event_type") == "refactor_txn_rolled_back"]
        assert len(rollback_events) == 0


# ---------------------------------------------------------------------------
# VAL-REF-023: Close idempotency and cleanup safety
# ---------------------------------------------------------------------------


class TestCloseIdempotency:
    """Repeat close calls on closed/rolled-back transactions are stable no-ops."""

    def test_close_on_already_closed_is_noop(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Make close succeed
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-001:0", "H-002:0", "H-003:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        result1 = manager.close(task_id="task-1", loop_id=12, force=True)
        assert result1.success is True
        assert result1.status == RefactorTransactionStatus.CLOSED_SUCCESS

        # Close again -> no-op
        result2 = manager.close(task_id="task-1", loop_id=13, force=True)
        assert result2.success is True
        assert result2.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert "already" in result2.reason.lower() or "no-op" in result2.reason.lower()

        # No duplicate events
        events = repo.list_events("task-1")
        close_events = [e for e in events if e.get("event_type") == "refactor_txn_closed_success"]
        assert len(close_events) == 1

    def test_close_on_already_rolled_back_is_noop(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Make close fail -> rollback
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        result1 = manager.close(task_id="task-1", loop_id=12, force=True)
        assert result1.status == RefactorTransactionStatus.ROLLED_BACK

        # Close again -> no-op
        result2 = manager.close(task_id="task-1", loop_id=13, force=True)
        assert result2.status == RefactorTransactionStatus.ROLLED_BACK
        assert "already" in result2.reason.lower() or "no-op" in result2.reason.lower()

        # No duplicate rollback events
        events = repo.list_events("task-1")
        rollback_events = [e for e in events if e.get("event_type") == "refactor_txn_rolled_back"]
        assert len(rollback_events) == 1


# ---------------------------------------------------------------------------
# VAL-REF-022: Disabled policy prevents all transaction behavior
# ---------------------------------------------------------------------------


class TestDisabledPolicy:
    """Disabled policy ignores stale transaction state."""

    def test_disabled_policy_no_open(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        policy = HungerPolicy(refactor_transactions_enabled=False)
        _setup_task(repo, policy=policy)
        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is False
        assert repo.get_open_refactor_transaction("task-1") is None

    def test_disabled_policy_no_close_on_stale_row(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        """With disabled policy, stale open rows are not settled."""
        # First enable, open a transaction, then disable
        policy_on = HungerPolicy(refactor_transactions_enabled=True)
        _setup_task(repo, policy=policy_on)
        _write_best_files(ws, "task-1", {"file1.py": "content"})
        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Disable policy
        policy_off = HungerPolicy(refactor_transactions_enabled=False)
        repo.set_hunger_policy("task-1", policy_off)

        # Try to close - should be a no-op due to disabled policy
        close_result = manager.close(task_id="task-1", loop_id=15, force=True)
        assert close_result.success is False
        assert "disabled" in close_result.reason.lower()

        # Transaction should remain open (not settled)
        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.OPEN

    def test_disabled_policy_settle_if_due_noop(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        """settle_if_due is a no-op when policy is disabled."""
        policy_on = HungerPolicy(refactor_transactions_enabled=True, refactor_deadline_loops=3)
        _setup_task(repo, policy=policy_on)
        _write_best_files(ws, "task-1", {"file1.py": "content"})
        manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Disable policy
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=False))

        # Even if deadline is past, settle_if_due should not fire
        result = manager.settle_if_due(task_id="task-1", current_loop=100)
        assert result is None  # No settlement occurred

        # Transaction remains open
        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.OPEN


# ---------------------------------------------------------------------------
# VAL-REF-027: Close-success persistence is atomic and retryable
# ---------------------------------------------------------------------------


class TestCloseSuccessAtomicity:
    """Close-success persistence must be atomic and retryable."""

    def test_close_success_event_persisted_atomically(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-001:0", "H-002:0", "H-003:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert result.success is True
        assert result.status == RefactorTransactionStatus.CLOSED_SUCCESS

        # Both status update and event should be present
        txn = repo.get_refactor_transaction(open_result.transaction.transaction_id)  # type: ignore[union-attr]
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.CLOSED_SUCCESS

        events = repo.list_events("task-1")
        close_events = [e for e in events if e.get("event_type") == "refactor_txn_closed_success"]
        assert len(close_events) == 1


# ---------------------------------------------------------------------------
# VAL-REF-018: settle_if_due closes due transactions
# ---------------------------------------------------------------------------


class TestSettleIfDue:
    """Settlement at deadline closes transactions exactly once."""

    def test_settle_if_due_not_yet_due(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        # deadline = 10 + 3 = 13

        result = manager.settle_if_due(task_id="task-1", current_loop=11)
        assert result is None  # Not yet due

        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.OPEN

    def test_settle_if_due_at_deadline(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        # deadline = 13

        # At deadline, close should happen
        result = manager.settle_if_due(task_id="task-1", current_loop=13)
        assert result is not None
        # Without strict superset, should roll back
        assert result.status == RefactorTransactionStatus.ROLLED_BACK

        # No open transaction after settlement
        assert repo.get_open_refactor_transaction("task-1") is None

    def test_settle_if_due_no_open_transaction(
        self,
        repo: InMemoryRepository,
        manager: RefactorTransactionManager,
    ) -> None:
        _setup_task(repo)
        result = manager.settle_if_due(task_id="task-1", current_loop=20)
        assert result is None


# ---------------------------------------------------------------------------
# VAL-REF-024: Audit events stable and non-secret
# ---------------------------------------------------------------------------


class TestAuditEvents:
    """Transaction events have stable, non-secret payloads."""

    def test_rejected_open_emits_audit_event(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        policy = HungerPolicy(refactor_transactions_enabled=False)
        _setup_task(repo, policy=policy)

        result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert result.success is False

        events = repo.list_events("task-1")
        rejected_events = [e for e in events if e.get("event_type") == "refactor_txn_open_rejected"]
        assert len(rejected_events) == 1
        payload = rejected_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["task_id"] == "task-1"
        assert "reason" in payload
        # No secrets
        for val in payload.values():
            if isinstance(val, str):
                assert "password" not in val.lower()
                assert "secret" not in val.lower()
                assert "api_key" not in val.lower()
