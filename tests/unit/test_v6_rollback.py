"""SQLite v6 rollback script tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import hungerloop.repository.sqlite_repo as sqlite_repo_module
from hungerloop.repository import migrations as migrations_pkg
from hungerloop.repository.sqlite_migrator import SQLiteMigrator
from hungerloop.repository.sqlite_repo import SQLiteRepository

LEGACY_TABLES = (
    "tasks",
    "hunger_items",
    "hunger_ledgers",
    "validation_reports",
    "evidence",
    "loop_traces",
    "stop_reports",
)
V6_TABLES = {
    "missions",
    "mission_phases",
    "mission_features",
    "worker_handoffs",
    "validation_assertions",
}
_FIXED_TS = "2026-05-18T00:00:00+00:00"


def _real_migrations_dir() -> Path:
    return Path(migrations_pkg.__file__).parent


def _read_pragma(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return int(row[0]) if row else 0


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _seed_v5_db(db_path: Path) -> dict[str, int]:
    SQLiteMigrator(
        db_path,
        _real_migrations_dir(),
        latest_version=5,
    ).ensure_current(write_capable=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO tasks(task_id, goal, status, last_stop_reason, created_at, updated_at)
            VALUES ('t1', 'Goal', 'pending', NULL, ?, ?)
            """,
            (_FIXED_TS, _FIXED_TS),
        )
        conn.execute(
            "INSERT INTO hunger_ledgers(task_id, payload_json) VALUES ('t1', '{}')"
        )
        conn.execute(
            """
            INSERT INTO hunger_items(
              item_id,
              task_id,
              status,
              gap_score,
              priority,
              consecutive_failure_count,
              last_progress_loop_id,
              payload_json
            )
            VALUES ('H-001', 't1', 'open', 1.0, 1.0, 0, 1, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO validation_reports(validation_id, task_id, loop_id, verdict, payload_json)
            VALUES ('VAL-1', 't1', 1, 'passed', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO evidence(evidence_id, task_id, loop_id, evidence_type, payload_json)
            VALUES ('E-1', 't1', 1, 'tool_call', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO loop_traces(task_id, loop_id, phase, committed, payload_json)
            VALUES ('t1', 1, 'validate', 1, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO stop_reports(report_id, task_id, stop_reason, created_at, payload_json)
            VALUES (1, 't1', 'done', ?, '{}')
            """,
            (_FIXED_TS,),
        )
        conn.commit()
        return {table: _table_count(conn, table) for table in LEGACY_TABLES}


def _apply_rollback(db_path: Path) -> None:
    script = (
        _real_migrations_dir() / "v6_rollback.sql"
    ).read_text(encoding="utf-8")
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(script)
        conn.commit()


def test_rollback_drops_all_tables_and_resets_version(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    legacy_counts = _seed_v5_db(db)

    repo = SQLiteRepository.open(db)
    repo.close()
    _apply_rollback(db)

    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 5
        assert V6_TABLES.isdisjoint(_table_names(conn))
        for table, count in legacy_counts.items():
            assert _table_count(conn, table) == count


def test_rollback_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db)

    repo = SQLiteRepository.open(db)
    repo.close()

    _apply_rollback(db)
    _apply_rollback(db)

    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 5
        assert V6_TABLES.isdisjoint(_table_names(conn))


def test_rollback_v0_5f_can_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db)

    repo = SQLiteRepository.open(db)
    repo.close()
    _apply_rollback(db)

    class LegacySQLiteMigrator(SQLiteMigrator):
        def __init__(self, db_path: Path, migrations_dir: Path) -> None:
            super().__init__(db_path, migrations_dir, latest_version=5)

    monkeypatch.setattr(
        sqlite_repo_module,
        "SQLiteMigrator",
        LegacySQLiteMigrator,
    )
    legacy_repo = SQLiteRepository.open(db)
    task = legacy_repo.get_task("t1")
    legacy_repo.close()

    assert task is not None
    assert task.raw_goal == "Goal"
