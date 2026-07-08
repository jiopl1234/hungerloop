"""SQLite v7 refactor-transactions migration tests (VAL-REF-004).

Tests forward migration from v6 to v7, fresh v7 initialization,
idempotent re-run, schema/index/uniqueness inspection, data preservation,
and status synchronization between payload JSON and indexed column.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.refactor import (
    RefactorTransaction,
    RefactorTransactionStatus,
)
from hungerloop.repository import migrations as migrations_pkg
from hungerloop.repository.sqlite_migrator import LATEST_VERSION, SQLiteMigrator
from hungerloop.repository.sqlite_repo import SQLiteRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_migrations_dir() -> Path:
    return Path(migrations_pkg.__file__).parent


def _read_pragma(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return int(row[0]) if row else 0


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_v6_db(db_path: Path) -> None:
    """Create a DB at v6 by running migrations up to v6."""
    SQLiteMigrator(
        db_path,
        _real_migrations_dir(),
        latest_version=6,
    ).ensure_current(write_capable=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO tasks(task_id, goal, status, last_stop_reason, created_at, updated_at)
            VALUES ('t1', 'Goal', 'pending', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO hunger_ledgers(task_id, payload_json) VALUES ('t1', '{}')"
        )
        conn.execute(
            """
            INSERT INTO hunger_items(
              item_id, task_id, status, gap_score, priority,
              consecutive_failure_count, last_progress_loop_id, payload_json
            )
            VALUES ('H-001', 't1', 'open', 1.0, 1.0, 0, 1, '{}')
            """
        )
        conn.commit()


def _make_best_state(task_id: str = "t1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="bs-001",
        summary="baseline",
        accepted_check_keys=["H-001:0"],
        updated_at_loop=3,
    )


def _make_txn_json(
    task_id: str = "t1",
    transaction_id: str = "txn-001",
    status: str = "open",
    opening_loop: int = 5,
) -> str:
    txn = RefactorTransaction(
        transaction_id=transaction_id,
        task_id=task_id,
        opening_loop=opening_loop,
        deadline_loop=opening_loop + 3,
        declared_regression_keys=["H-001:0"],
        baseline_accepted_check_keys=["H-001:0"],
        baseline_accepted_check_count=1,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=f".txn_{transaction_id}",
        status=RefactorTransactionStatus(status),
    )
    return txn.model_dump_json()


# ---------------------------------------------------------------------------
# VAL-REF-004: Migration from v6 to v7
# ---------------------------------------------------------------------------


class TestV6ToV7Migration:
    def test_migration_from_v6_to_v7(self, tmp_path: Path) -> None:
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        # Migrate to v7
        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            assert _read_pragma(conn, "user_version") == 7
            tables = _table_names(conn)
            assert "refactor_transactions" in tables

    def test_v7_table_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            columns = _table_columns(conn, "refactor_transactions")
            # Required columns
            assert "transaction_id" in columns
            assert "task_id" in columns
            assert "status" in columns  # indexed status column
            assert "payload_json" in columns  # full JSON payload
            assert "opening_loop" in columns
            assert "deadline_loop" in columns

    def test_v7_indexes_created(self, tmp_path: Path) -> None:
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            indices = _index_names(conn)
            # At minimum, task and status indexes should exist
            # We check that index names contain expected patterns
            idx_list = list(indices)
            # There should be an index on task_id
            assert any("task" in idx.lower() or "refactor" in idx.lower() for idx in idx_list), \
                f"Expected refactor transaction indexes, got: {idx_list}"

    def test_v7_open_uniqueness_guard(self, tmp_path: Path) -> None:
        """The partial unique index prevents more than one open transaction per task."""
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            # Insert first open transaction
            payload1 = _make_txn_json(transaction_id="txn-1", status="open")
            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-1", "t1", "open", 5, 8, payload1),
            )
            conn.commit()

            # Insert second open transaction for same task should fail
            payload2 = _make_txn_json(transaction_id="txn-2", status="open", opening_loop=6)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO refactor_transactions(
                        transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("txn-2", "t1", "open", 6, 9, payload2),
                )

    def test_v7_multiple_closed_transactions_allowed(self, tmp_path: Path) -> None:
        """Multiple closed_success or rolled_back transactions for same task are allowed."""
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            payload1 = _make_txn_json(transaction_id="txn-1", status="closed_success")
            payload2 = _make_txn_json(transaction_id="txn-2", status="rolled_back")

            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-1", "t1", "closed_success", 5, 8, payload1),
            )
            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-2", "t1", "rolled_back", 9, 12, payload2),
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM refactor_transactions WHERE task_id = ?",
                ("t1",),
            ).fetchone()
            assert count[0] == 2

    def test_v7_open_for_different_tasks_allowed(self, tmp_path: Path) -> None:
        """Two open transactions for different tasks are allowed."""
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, goal, status, created_at, updated_at)
                VALUES ('t2', 'Other', 'pending', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            payload1 = _make_txn_json(task_id="t1", transaction_id="txn-1", status="open")
            payload2 = _make_txn_json(
                task_id="t2", transaction_id="txn-2", status="open", opening_loop=1
            )

            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-1", "t1", "open", 5, 8, payload1),
            )
            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-2", "t2", "open", 1, 4, payload2),
            )
            conn.commit()

    def test_migration_preserves_existing_data(self, tmp_path: Path) -> None:
        """Migrating v6 to v7 preserves existing data."""
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            # Existing data is intact
            task = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", ("t1",)
            ).fetchone()
            assert task is not None

            item = conn.execute(
                "SELECT item_id FROM hunger_items WHERE item_id = ?", ("H-001",)
            ).fetchone()
            assert item is not None

            ledger = conn.execute(
                "SELECT task_id FROM hunger_ledgers WHERE task_id = ?", ("t1",)
            ).fetchone()
            assert ledger is not None


# ---------------------------------------------------------------------------
# VAL-REF-004: Idempotent re-run
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    def test_rerun_migration_preserves_data_and_version(self, tmp_path: Path) -> None:
        db = tmp_path / "hungerloop.sqlite"
        _seed_v6_db(db)

        # First migration to v7
        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        # Insert a transaction row
        with sqlite3.connect(str(db)) as conn:
            payload = _make_txn_json(transaction_id="txn-1", status="closed_success")
            conn.execute(
                """
                INSERT INTO refactor_transactions(
                    transaction_id, task_id, status, opening_loop, deadline_loop, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("txn-1", "t1", "closed_success", 5, 8, payload),
            )
            conn.commit()

        # Re-run migrations (should be no-op at v7)
        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            assert _read_pragma(conn, "user_version") == 7
            # Data is preserved
            row = conn.execute(
                "SELECT transaction_id, status FROM refactor_transactions WHERE transaction_id = ?",
                ("txn-1",),
            ).fetchone()
            assert row is not None
            assert row[0] == "txn-1"
            assert row[1] == "closed_success"

    def test_fresh_db_initializes_at_v7(self, tmp_path: Path) -> None:
        """A fresh DB should initialize directly at v7."""
        db = tmp_path / "hungerloop.sqlite"
        SQLiteMigrator(
            db,
            _real_migrations_dir(),
            latest_version=7,
        ).ensure_current(write_capable=True)

        with sqlite3.connect(str(db)) as conn:
            assert _read_pragma(conn, "user_version") == 7
            assert "refactor_transactions" in _table_names(conn)


# ---------------------------------------------------------------------------
# VAL-REF-004: Status synchronization
# ---------------------------------------------------------------------------


class TestStatusSynchronization:
    def test_indexed_status_matches_payload_status(self, tmp_path: Path) -> None:
        """When saving via repository, indexed status and JSON status must match."""
        db_path = tmp_path / "hungerloop.sqlite"
        repo = SQLiteRepository.open(db_path)
        repo.create_task("t1", "Goal")

        txn = RefactorTransaction(
            transaction_id="txn-001",
            task_id="t1",
            opening_loop=5,
            deadline_loop=8,
            declared_regression_keys=["H-001:0"],
            baseline_accepted_check_keys=["H-001:0"],
            baseline_accepted_check_count=1,
            baseline_best_state=_make_best_state("t1"),
            snapshot_path=".txn_txn-001",
            status=RefactorTransactionStatus.OPEN,
        )
        repo.save_refactor_transaction(txn)

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT status, payload_json FROM refactor_transactions WHERE transaction_id = ?",
                ("txn-001",),
            ).fetchone()
            assert row is not None
            indexed_status = row[0]
            payload = json.loads(row[1])
            payload_status = payload["status"]

            assert indexed_status == "open"
            assert payload_status == "open"
            assert indexed_status == payload_status

        repo.close()

    def test_status_synchronized_after_update(self, tmp_path: Path) -> None:
        """After status update, indexed status and payload JSON status remain synchronized."""
        db_path = tmp_path / "hungerloop.sqlite"
        repo = SQLiteRepository.open(db_path)
        repo.create_task("t1", "Goal")

        txn = RefactorTransaction(
            transaction_id="txn-001",
            task_id="t1",
            opening_loop=5,
            deadline_loop=8,
            declared_regression_keys=["H-001:0"],
            baseline_accepted_check_keys=["H-001:0"],
            baseline_accepted_check_count=1,
            baseline_best_state=_make_best_state("t1"),
            snapshot_path=".txn_txn-001",
            status=RefactorTransactionStatus.OPEN,
        )
        repo.save_refactor_transaction(txn)

        repo.update_refactor_transaction_status(
            transaction_id="txn-001",
            status=RefactorTransactionStatus.ROLLED_BACK,
            closed_loop=9,
            close_reason="failed",
        )

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT status, payload_json FROM refactor_transactions WHERE transaction_id = ?",
                ("txn-001",),
            ).fetchone()
            assert row is not None
            indexed_status = row[0]
            payload = json.loads(row[1])
            payload_status = payload["status"]

            assert indexed_status == "rolled_back"
            assert payload_status == "rolled_back"
            assert indexed_status == payload_status

        repo.close()


# ---------------------------------------------------------------------------
# VAL-REF-004: LATEST_VERSION is bumped
# ---------------------------------------------------------------------------


class TestLatestVersionBumped:
    def test_latest_version_is_7(self) -> None:
        assert LATEST_VERSION == 7
