from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hungerloop.models.events import EventType
from hungerloop.repository.sqlite_repo import SQLiteRepository

_BUDGET_MS = 200.0


def _seed_v5_db(db_path: Path, rows: int = 10_000) -> None:
    repo = SQLiteRepository.open(db_path)
    repo.create_task("perf-migration", "migration perf")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.execute("PRAGMA application_id = 424242")
        for index in range(rows):
            conn.execute(
                """
                INSERT INTO hunger_items(
                  item_id, task_id, status, gap_score, priority,
                  consecutive_failure_count, last_progress_loop_id, payload_json
                )
                VALUES (?, 'perf-migration', 'open', 1.0, 1.0, 0, 1, '{}')
                """,
                (f"H-{index:05d}",),
            )
        conn.commit()


@pytest.mark.perf
def test_v6_migration_under_200ms(tmp_path: Path) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    _seed_v5_db(db_path)

    repo = SQLiteRepository.open(db_path)
    repo.close()

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT json_extract(payload_json, '$.duration_ms') "
            "FROM events WHERE event_type = ? ORDER BY event_id DESC LIMIT 1",
            (EventType.MIGRATION_APPLIED.value,),
        ).fetchone()

    duration_ms = float(row[0]) if row and row[0] is not None else 0.0
    assert duration_ms <= _BUDGET_MS
