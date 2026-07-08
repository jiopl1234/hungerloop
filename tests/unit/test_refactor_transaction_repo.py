"""Repository contract tests for RefactorTransaction persistence.

Covers VAL-REF-003 (lifecycle consistency) and VAL-REF-021 (single-open
and lifecycle integrity) across both InMemoryRepository and SQLiteRepository.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.refactor import (
    RefactorTransaction,
    RefactorTransactionStatus,
)
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_best_state(task_id: str = "task-1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="bs-001",
        summary="baseline",
        accepted_check_keys=["H-001:0", "H-002:0"],
        updated_at_loop=3,
    )


def _make_open_txn(
    task_id: str = "task-1",
    transaction_id: str = "txn-001",
    opening_loop: int = 5,
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id=transaction_id,
        task_id=task_id,
        opening_loop=opening_loop,
        deadline_loop=opening_loop + 3,
        declared_regression_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=f".txn_{transaction_id}",
        status=RefactorTransactionStatus.OPEN,
    )


def _make_closed_txn(
    task_id: str = "task-1",
    transaction_id: str = "txn-001",
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id=transaction_id,
        task_id=task_id,
        opening_loop=5,
        deadline_loop=8,
        declared_regression_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=f".txn_{transaction_id}",
        status=RefactorTransactionStatus.CLOSED_SUCCESS,
        closed_loop=10,
        close_reason="all regressions recovered",
    )


def _make_rolled_back_txn(
    task_id: str = "task-1",
    transaction_id: str = "txn-001",
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id=transaction_id,
        task_id=task_id,
        opening_loop=5,
        deadline_loop=8,
        declared_regression_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=f".txn_{transaction_id}",
        status=RefactorTransactionStatus.ROLLED_BACK,
        closed_loop=9,
        close_reason="unrecovered regressions",
    )


# ---------------------------------------------------------------------------
# Parametrized fixtures: both repos must pass the same contract
# ---------------------------------------------------------------------------

repo_factories = [
    pytest.param(
        lambda: InMemoryRepository(),
        id="in-memory",
    ),
    pytest.param(
        lambda path: SQLiteRepository.open(path / "hungerloop.sqlite"),
        id="sqlite",
    ),
]


def _make_repo(factory, tmp_path: Path):
    import inspect

    sig = inspect.signature(factory)
    if len(sig.parameters) == 0:
        return factory()
    return factory(tmp_path)


@pytest.fixture(params=repo_factories)
def repo(request, tmp_path: Path):
    r = _make_repo(request.param, tmp_path)
    r.create_task("task-1", "Goal")
    yield r
    close = getattr(r, "close", None)
    if close is not None:
        close()


# ---------------------------------------------------------------------------
# VAL-REF-003: Save and fetch by id
# ---------------------------------------------------------------------------


class TestSaveAndFetch:
    def test_save_and_fetch_open_transaction(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)
        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.transaction_id == "txn-001"
        assert fetched.task_id == "task-1"
        assert fetched.status == RefactorTransactionStatus.OPEN
        assert fetched.declared_regression_keys == ["H-001:0", "H-002:0"]
        assert fetched.deadline_loop == 8
        assert fetched.opening_loop == 5
        assert fetched.baseline_accepted_check_count == 2
        assert fetched.snapshot_path == ".txn_txn-001"

    def test_fetch_nonexistent_returns_none(self, repo) -> None:
        assert repo.get_refactor_transaction("nonexistent") is None

    def test_save_updates_existing_transaction(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        updated = txn.model_copy(
            update={
                "status": RefactorTransactionStatus.CLOSED_SUCCESS,
                "closed_loop": 10,
                "close_reason": "recovered",
            }
        )
        repo.save_refactor_transaction(updated)

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert fetched.closed_loop == 10
        assert fetched.close_reason == "recovered"


# ---------------------------------------------------------------------------
# VAL-REF-003: Fetch open transaction for a task
# ---------------------------------------------------------------------------


class TestGetOpenTransaction:
    def test_get_open_transaction_returns_open(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)
        open_txn = repo.get_open_refactor_transaction("task-1")
        assert open_txn is not None
        assert open_txn.transaction_id == "txn-001"
        assert open_txn.status == RefactorTransactionStatus.OPEN

    def test_get_open_returns_none_after_closed_success(self, repo) -> None:
        txn = _make_closed_txn()
        repo.save_refactor_transaction(txn)
        assert repo.get_open_refactor_transaction("task-1") is None

    def test_get_open_returns_none_after_rolled_back(self, repo) -> None:
        txn = _make_rolled_back_txn()
        repo.save_refactor_transaction(txn)
        assert repo.get_open_refactor_transaction("task-1") is None

    def test_get_open_returns_none_when_no_transactions(self, repo) -> None:
        assert repo.get_open_refactor_transaction("task-1") is None


# ---------------------------------------------------------------------------
# VAL-REF-003: List task transactions in opening-loop order
# ---------------------------------------------------------------------------


class TestListTransactions:
    def test_list_transactions_in_opening_loop_order(self, repo) -> None:
        txn1 = _make_open_txn(transaction_id="txn-1", opening_loop=5)
        txn2 = _make_closed_txn(transaction_id="txn-2")
        txn2 = txn2.model_copy(update={"opening_loop": 3})
        txn3 = _make_rolled_back_txn(transaction_id="txn-3")
        txn3 = txn3.model_copy(update={"opening_loop": 7})

        repo.save_refactor_transaction(txn2)
        repo.save_refactor_transaction(txn1)
        repo.save_refactor_transaction(txn3)

        txns = repo.list_refactor_transactions("task-1")
        assert len(txns) == 3
        assert txns[0].transaction_id == "txn-2"
        assert txns[0].opening_loop == 3
        assert txns[1].transaction_id == "txn-1"
        assert txns[1].opening_loop == 5
        assert txns[2].transaction_id == "txn-3"
        assert txns[2].opening_loop == 7

    def test_list_empty_when_no_transactions(self, repo) -> None:
        assert repo.list_refactor_transactions("task-1") == []

    def test_list_only_returns_transactions_for_task(self, repo) -> None:
        repo.create_task("task-2", "Other")
        txn1 = _make_open_txn(task_id="task-1", transaction_id="txn-1")
        txn2 = _make_open_txn(
            task_id="task-2", transaction_id="txn-2", opening_loop=1
        )
        repo.save_refactor_transaction(txn1)
        repo.save_refactor_transaction(txn2)

        task1_txns = repo.list_refactor_transactions("task-1")
        assert len(task1_txns) == 1
        assert task1_txns[0].transaction_id == "txn-1"

        task2_txns = repo.list_refactor_transactions("task-2")
        assert len(task2_txns) == 1
        assert task2_txns[0].transaction_id == "txn-2"


# ---------------------------------------------------------------------------
# VAL-REF-003: Update status and close metadata
# ---------------------------------------------------------------------------


class TestUpdateTransactionStatus:
    def test_update_to_closed_success(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="all regressions recovered",
        )

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert fetched.closed_loop == 10
        assert fetched.close_reason == "all regressions recovered"

    def test_update_to_rolled_back(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.ROLLED_BACK,
            closed_loop=9,
            close_reason="unrecovered regressions",
        )

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.ROLLED_BACK
        assert fetched.closed_loop == 9
        assert fetched.close_reason == "unrecovered regressions"

    def test_update_nonexistent_returns_none(self, repo) -> None:
        """Updating a nonexistent transaction is a safe no-op."""
        result = repo.update_refactor_transaction_status(
            transaction_id="nonexistent",
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="done",
        )
        assert result is None

    def test_update_preserves_other_fields(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="recovered",
        )

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.task_id == "task-1"
        assert fetched.opening_loop == 5
        assert fetched.deadline_loop == 8
        assert fetched.declared_regression_keys == ["H-001:0", "H-002:0"]
        assert fetched.baseline_accepted_check_count == 2
        assert fetched.snapshot_path == ".txn_txn-001"

    def test_invalid_status_string_rejected(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        with pytest.raises((ValueError, TypeError)):
            repo.update_refactor_transaction_status(
                transaction_id="txn-001",
                status="invalid_status",  # type: ignore[arg-type]
                closed_loop=10,
                close_reason="done",
            )

    def test_payload_json_and_indexed_status_synchronized(self, repo) -> None:
        """After update, the indexed status column must match the payload JSON status."""
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.ROLLED_BACK,
            closed_loop=9,
            close_reason="failed",
        )

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.ROLLED_BACK

        open_txn = repo.get_open_refactor_transaction("task-1")
        assert open_txn is None


# ---------------------------------------------------------------------------
# VAL-REF-021: Single-open enforcement
# ---------------------------------------------------------------------------


class TestSingleOpenEnforcement:
    def test_second_open_for_same_task_rejected(self, repo) -> None:
        txn1 = _make_open_txn(transaction_id="txn-1")
        repo.save_refactor_transaction(txn1)

        txn2 = _make_open_txn(
            transaction_id="txn-2", opening_loop=6
        )
        with pytest.raises((ValueError, RuntimeError)):
            repo.save_refactor_transaction(txn2)

    def test_multiple_non_open_transactions_allowed(self, repo) -> None:
        txn1 = _make_closed_txn(transaction_id="txn-1")
        txn2 = _make_rolled_back_txn(transaction_id="txn-2")
        txn3 = _make_closed_txn(transaction_id="txn-3")

        # All non-open, should not conflict
        repo.save_refactor_transaction(txn1)
        repo.save_refactor_transaction(txn2)
        repo.save_refactor_transaction(txn3)

        txns = repo.list_refactor_transactions("task-1")
        assert len(txns) == 3

    def test_open_after_close_allowed(self, repo) -> None:
        txn1 = _make_closed_txn(transaction_id="txn-1")
        repo.save_refactor_transaction(txn1)

        txn2 = _make_open_txn(transaction_id="txn-2", opening_loop=10)
        repo.save_refactor_transaction(txn2)

        open_txn = repo.get_open_refactor_transaction("task-1")
        assert open_txn is not None
        assert open_txn.transaction_id == "txn-2"

    def test_open_after_rolled_back_allowed(self, repo) -> None:
        txn1 = _make_rolled_back_txn(transaction_id="txn-1")
        repo.save_refactor_transaction(txn1)

        txn2 = _make_open_txn(transaction_id="txn-2", opening_loop=10)
        repo.save_refactor_transaction(txn2)

        open_txn = repo.get_open_refactor_transaction("task-1")
        assert open_txn is not None
        assert open_txn.transaction_id == "txn-2"

    def test_open_for_different_tasks_allowed(self, repo) -> None:
        repo.create_task("task-2", "Other")
        txn1 = _make_open_txn(task_id="task-1", transaction_id="txn-1")
        txn2 = _make_open_txn(
            task_id="task-2", transaction_id="txn-2", opening_loop=1
        )
        repo.save_refactor_transaction(txn1)
        repo.save_refactor_transaction(txn2)

        open1 = repo.get_open_refactor_transaction("task-1")
        open2 = repo.get_open_refactor_transaction("task-2")
        assert open1 is not None and open1.transaction_id == "txn-1"
        assert open2 is not None and open2.transaction_id == "txn-2"


# ---------------------------------------------------------------------------
# VAL-REF-021: Lifecycle integrity after invalid operations
# ---------------------------------------------------------------------------


class TestLifecycleIntegrity:
    def test_mismatched_transaction_id_on_update_is_noop(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        result = repo.update_refactor_transaction_status(
            transaction_id="wrong-id",
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="done",
        )
        assert result is None

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.OPEN
        assert fetched.closed_loop is None

    def test_status_update_to_same_status_is_safe(self, repo) -> None:
        """Updating to the same status should be safe (idempotent)."""
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.OPEN,
            closed_loop=None,
            close_reason=None,
        )

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.OPEN

    def test_round_trip_preserves_baseline_best_state(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.baseline_best_state.task_id == "task-1"
        assert fetched.baseline_best_state.state_id == "bs-001"
        assert fetched.baseline_best_state.accepted_check_keys == [
            "H-001:0",
            "H-002:0",
        ]

    def test_round_trip_preserves_declared_keys(self, repo) -> None:
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)

        fetched = repo.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.declared_regression_keys == ["H-001:0", "H-002:0"]
        assert fetched.baseline_accepted_check_keys == ["H-001:0", "H-002:0"]


# ---------------------------------------------------------------------------
# SQLite-specific: reopen round-trip
# ---------------------------------------------------------------------------


class TestSQLiteReopen:
    def test_sqlite_reopen_preserves_transaction(self, tmp_path: Path) -> None:
        db_path = tmp_path / "hungerloop.sqlite"
        repo = SQLiteRepository.open(db_path)
        repo.create_task("task-1", "Goal")
        txn = _make_open_txn()
        repo.save_refactor_transaction(txn)
        repo.close()

        reopened = SQLiteRepository.open(db_path)
        fetched = reopened.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.transaction_id == "txn-001"
        assert fetched.status == RefactorTransactionStatus.OPEN
        assert fetched.declared_regression_keys == ["H-001:0", "H-002:0"]
        assert fetched.baseline_accepted_check_count == 2

        open_txn = reopened.get_open_refactor_transaction("task-1")
        assert open_txn is not None
        assert open_txn.transaction_id == "txn-001"
        reopened.close()

    def test_sqlite_reopen_preserves_closed_transaction(self, tmp_path: Path) -> None:
        db_path = tmp_path / "hungerloop.sqlite"
        repo = SQLiteRepository.open(db_path)
        repo.create_task("task-1", "Goal")
        txn = _make_closed_txn()
        repo.save_refactor_transaction(txn)
        repo.close()

        reopened = SQLiteRepository.open(db_path)
        fetched = reopened.get_refactor_transaction("txn-001")
        assert fetched is not None
        assert fetched.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert fetched.closed_loop == 10
        assert fetched.close_reason == "all regressions recovered"

        assert reopened.get_open_refactor_transaction("task-1") is None
        reopened.close()
