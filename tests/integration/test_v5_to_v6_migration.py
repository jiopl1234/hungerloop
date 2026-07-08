from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.events import EventType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.repository.sqlite_repo import SQLiteRepository
from tests.unit.test_v6_migration import (
    LEGACY_TABLES,
    _read_pragma,
    _seed_v5_db,
    _table_count,
)


def test_legacy_loop_works_post_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    legacy_counts, _application_id = _seed_v5_db(db_path)

    repo = SQLiteRepository.open(db_path)
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-001",
                    title="legacy report",
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.FILE_EXISTS,
                            params={"path": "legacy.txt"},
                        )
                    ],
                )
            ],
        ),
    )
    result = CliRunner().invoke(
        cli,
        ["run", "t1", "--max-loops", "1", "--reset"],
        obj=CliContext(repo=repo, workspace_root=tmp_path),
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(str(db_path)) as conn:
        assert _read_pragma(conn, "user_version") == 7
        for table in LEGACY_TABLES:
            assert _table_count(conn, table) >= legacy_counts[table]
        mission_event_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM events
            WHERE task_id = 't1' AND event_type LIKE 'mission.%'
            """
        ).fetchone()[0]
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            (EventType.MIGRATION_APPLIED.value,),
        ).fetchone()[0]
    repo.close()

    assert mission_event_count == 0
    # 5 events from seeding (v0->v1..v4->v5) + 2 from open (v5->v6, v6->v7).
    assert migration_count == 7
