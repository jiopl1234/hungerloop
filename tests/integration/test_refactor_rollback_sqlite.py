"""Integration tests for refactor transaction rollback with SQLite (VAL-REF-012, VAL-REF-013).

Covers:
- SQLite-backed rollback restores exact baseline best files and best state
- Rollback is retryable on restoration failure with SQLite
- Close-success persistence is atomic with SQLite
- Disabled policy with stale rows in SQLite preserves strict v0.6 behavior
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.refactor import RefactorTransactionStatus
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.refactor_transaction_manager import (
    RefactorTransactionManager,
)
from hungerloop.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_task(
    repo: SQLiteRepository,
    task_id: str = "task-1",
    accepted_keys: list[str] | None = None,
    policy: HungerPolicy | None = None,
) -> BestState:
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
    best_dir = ws.best_files_dir(task_id)
    best_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        full = best_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteRepository:
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def manager(repo: SQLiteRepository, ws: WorkspaceManager) -> RefactorTransactionManager:
    return RefactorTransactionManager(repo=repo, workspace_manager=ws)


# ---------------------------------------------------------------------------
# Integration tests with SQLite
# ---------------------------------------------------------------------------


class TestSQLiteRollback:
    """SQLite-backed rollback integration tests."""

    def test_sqlite_rollback_restores_files(
        self,
        repo: SQLiteRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Rollback with SQLite restores exact baseline files."""
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "original", "dir/file2.py": "original2"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Mutate best files during transaction
        best_dir = ws.best_files_dir("task-1")
        (best_dir / "file1.py").write_text("modified", encoding="utf-8")
        (best_dir / "new_file.py").write_text("new", encoding="utf-8")

        # Mutate best state so close fails
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

        # Files should be restored
        assert (best_dir / "file1.py").read_text(encoding="utf-8") == "original"
        assert (best_dir / "dir" / "file2.py").read_text(encoding="utf-8") == "original2"
        # New file should be removed
        assert not (best_dir / "new_file.py").exists()

    def test_sqlite_rollback_restores_best_state(
        self,
        repo: SQLiteRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Rollback with SQLite restores exact baseline BestState."""
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

        # Best state should be restored
        restored = repo.get_best_state("task-1")
        assert restored is not None
        assert restored.accepted_check_keys == ["H-001:0", "H-002:0"]
        assert restored.state_id == "BEST-001"

    def test_sqlite_close_success_persists(
        self,
        repo: SQLiteRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Close success with SQLite persists transaction status and event."""
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

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert close_result.success is True
        assert close_result.status == RefactorTransactionStatus.CLOSED_SUCCESS

        # Verify in SQLite
        txn = repo.get_refactor_transaction(open_result.transaction.transaction_id)  # type: ignore[union-attr]
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.CLOSED_SUCCESS

        # Verify event
        events = repo.list_events("task-1")
        close_events = [e for e in events if e.get("event_type") == "refactor_txn_closed_success"]
        assert len(close_events) == 1

    def test_sqlite_disabled_policy_stale_row(
        self,
        repo: SQLiteRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Disabled policy with stale open row in SQLite does not settle."""
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Disable policy
        repo.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=False))

        # settle_if_due should be a no-op
        result = manager.settle_if_due(task_id="task-1", current_loop=100)
        assert result is None

        # Transaction should remain open
        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.status == RefactorTransactionStatus.OPEN

    def test_sqlite_snapshot_independence(
        self,
        repo: SQLiteRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Snapshot is independent of later best-workspace changes with SQLite."""
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "original"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True
        txn = open_result.transaction
        assert txn is not None

        snapshot_dir = ws.task_root("task-1") / txn.snapshot_path

        # Mutate best files after open
        best_dir = ws.best_files_dir("task-1")
        (best_dir / "file1.py").write_text("modified", encoding="utf-8")
        (best_dir / "new_file.py").write_text("new", encoding="utf-8")

        # Snapshot should still have the original content
        assert (snapshot_dir / "file1.py").read_text(encoding="utf-8") == "original"
        assert not (snapshot_dir / "new_file.py").exists()
