"""Tests for VAL-CROSS-017: Rolled-back progress excluded from success summaries.

When a refactor transaction rolls back, the accepted-check state derived
from rolled-back candidate progress must be excluded from user-facing
success summaries. This test verifies that after rollback, the best state
accepted_check_keys are restored to the baseline, and the stop report
reflects the baseline state, not the rolled-back progress.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import StopReason
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.refactor import RefactorTransactionStatus
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.refactor_transaction_manager import (
    RefactorTransactionManager,
)
from hungerloop.services.stop_report_builder import build_stop_report
from hungerloop.services.workspace_manager import WorkspaceManager


def _setup_task(
    repo: InMemoryRepository,
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
        summary="baseline",
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
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def manager(repo: InMemoryRepository, ws: WorkspaceManager) -> RefactorTransactionManager:
    return RefactorTransactionManager(repo=repo, workspace_manager=ws)


class TestRolledBackProgressExcluded:
    """Rolled-back progress is excluded from success summaries."""

    def test_rollback_restores_accepted_checks(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """After rollback, accepted_check_keys are restored to baseline."""
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Simulate progress during transaction that will be rolled back
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="during refactor",
            accepted_check_keys=["H-001:0", "H-002:0", "H-003:0", "H-004:0"],
            updated_at_loop=11,
        )
        repo.save_best_state(new_best)

        # Force close -> fails because H-001:0 not in current accepted
        # (simulating unrecovered regression)
        # Actually let's make it fail by not having strict superset
        failed_best = BestState(
            task_id="task-1",
            state_id="BEST-003",
            summary="failed",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(failed_best)

        close_result = manager.close(task_id="task-1", loop_id=12, force=True)
        assert close_result.status == RefactorTransactionStatus.ROLLED_BACK

        # Best state should be restored to baseline
        restored = repo.get_best_state("task-1")
        assert restored is not None
        assert set(restored.accepted_check_keys) == {"H-001:0", "H-002:0"}

    def test_stop_report_excludes_rolled_back_progress(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        manager: RefactorTransactionManager,
    ) -> None:
        """Stop report after rollback shows baseline accepted checks, not
        rolled-back progress."""
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        open_result = manager.open(
            task_id="task-1",
            loop_id=10,
            declared_regression_keys=["H-001:0"],
            rationale="refactoring",
        )
        assert open_result.success is True

        # Simulate failed close
        failed_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="failed",
            accepted_check_keys=["H-002:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(failed_best)

        manager.close(task_id="task-1", loop_id=12, force=True)

        # Build stop report
        report = build_stop_report(repo, "task-1", StopReason.HUNGER_EXPIRED)
        assert report.accepted_check_keys_count == 2  # baseline count
