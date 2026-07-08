"""SQLite v6 mission-runtime migration tests."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

import hungerloop.repository.sqlite_repo as sqlite_repo_module
from hungerloop.cli.main import cli
from hungerloop.models.events import EventType
from hungerloop.repository import migrations as migrations_pkg
from hungerloop.repository.migration_errors import MigrationFailedError
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
V6_INDICES = {
    "idx_phases_mission",
    "idx_features_phase",
    "idx_assertions_phase",
    "idx_handoffs_loop",
}
V6_COLUMNS = {
    "missions": {
        "mission_id",
        "task_id",
        "payload_json",
        "created_at",
        "updated_at",
    },
    "mission_phases": {
        "phase_id",
        "mission_id",
        "status",
        "payload_json",
    },
    "mission_features": {
        "feature_id",
        "mission_id",
        "phase_id",
        "hunger_item_id",
        "status",
        "payload_json",
    },
    "worker_handoffs": {
        "handoff_id",
        "task_id",
        "loop_id",
        "agent_id",
        "payload_json",
        "created_at",
    },
    "validation_assertions": {
        "assertion_id",
        "mission_id",
        "phase_id",
        "status",
        "payload_json",
    },
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


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _event_payloads(
    conn: sqlite3.Connection, event_type: EventType
) -> list[dict[str, object]]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type = ?
            ORDER BY event_id
            """,
            (event_type.value,),
        )
    ]


def _seed_v5_db(db_path: Path) -> tuple[dict[str, int], int]:
    SQLiteMigrator(
        db_path,
        _real_migrations_dir(),
        latest_version=5,
    ).ensure_current(write_capable=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA application_id = 424242")
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
        conn.execute(
            """
            INSERT INTO events(task_id, loop_id, event_type, payload_json, created_at)
            VALUES ('t1', 1, ?, '{}', ?)
            """,
            (EventType.LOOP_STARTED.value, _FIXED_TS),
        )
        conn.commit()
        return (
            {table: _table_count(conn, table) for table in LEGACY_TABLES},
            _read_pragma(conn, "application_id"),
        )


def _make_broken_v6_migrations_dir(tmp_path: Path) -> Path:
    broken_dir = tmp_path / "broken-migrations"
    shutil.copytree(_real_migrations_dir(), broken_dir)
    (broken_dir / "v6__mission_runtime.sql").write_text(
        """
        CREATE TABLE missions (
          mission_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        THIS IS NOT VALID SQL;
        PRAGMA user_version = 6;
        """,
        encoding="utf-8",
    )
    return broken_dir


def test_forward_migration_from_v5(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    legacy_counts, application_id = _seed_v5_db(db)

    repo = SQLiteRepository.open(db)
    repo.close()

    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 7
        assert _read_pragma(conn, "application_id") == application_id
        assert V6_TABLES <= _table_names(conn)
        assert V6_INDICES <= _index_names(conn)
        for table, expected_columns in V6_COLUMNS.items():
            assert _table_columns(conn, table) == expected_columns
        for table, count in legacy_counts.items():
            assert _table_count(conn, table) == count
        payloads = _event_payloads(conn, EventType.MIGRATION_APPLIED)

    # _seed_v5_db runs v0->v1..v4->v5 (5 events); open runs v5->v6, v6->v7.
    assert len(payloads) == 7
    # Verify v5->v6 and v6->v7 transitions are present.
    v5_to_v6 = [p for p in payloads if p["from_version"] == 5 and p["to_version"] == 6]
    v6_to_v7 = [p for p in payloads if p["from_version"] == 6 and p["to_version"] == 7]
    assert len(v5_to_v6) == 1
    assert len(v6_to_v7) == 1
    assert float(v5_to_v6[0]["duration_ms"]) >= 0
    assert float(v6_to_v7[0]["duration_ms"]) >= 0


def test_migration_idempotent_rerun_does_not_duplicate_event(
    tmp_path: Path,
) -> None:
    db = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db)

    repo = SQLiteRepository.open(db)
    repo.close()
    with sqlite3.connect(str(db)) as conn:
        first_count = len(_event_payloads(conn, EventType.MIGRATION_APPLIED))

    reopened = SQLiteRepository.open(db)
    reopened.close()
    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 7
        second_count = len(_event_payloads(conn, EventType.MIGRATION_APPLIED))

    # 5 events from seeding (v0->v1..v4->v5) + 2 from open (v5->v6, v6->v7).
    assert first_count == 7
    assert second_count == first_count


def test_failed_migration_atomic(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db)
    broken_dir = _make_broken_v6_migrations_dir(tmp_path)

    with pytest.raises(MigrationFailedError):
        SQLiteMigrator(
            db,
            broken_dir,
            latest_version=6,
        ).ensure_current(write_capable=True)

    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 5
        assert V6_TABLES.isdisjoint(_table_names(conn))
        assert len(_event_payloads(conn, EventType.MIGRATION_FAILED)) == 1


def test_cli_run_exits_5_on_v6_migration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db)
    broken_dir = _make_broken_v6_migrations_dir(tmp_path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sqlite_repo_module.migrations_pkg,
        "__file__",
        str(broken_dir / "__init__.py"),
    )
    result = CliRunner().invoke(cli, ["run", "t1"])

    assert result.exit_code == 5
    with sqlite3.connect(str(db)) as conn:
        assert _read_pragma(conn, "user_version") == 5
        assert len(_event_payloads(conn, EventType.MIGRATION_FAILED)) == 1
