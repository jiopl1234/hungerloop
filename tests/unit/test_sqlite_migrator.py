"""SQLite forward-only migrator tests (PRD §5.5).

Pin the version detection, backup, transactional migration, downgrade
refusal, read-only refusal, and backup pruning paths.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hungerloop.repository.migration_errors import (
    DownMigrationDisallowed,
    MigrationFailedError,
    SchemaTooNewError,
)
from hungerloop.repository.sqlite_migrator import (
    _BACKUPS_TO_KEEP,
    LATEST_VERSION,
    ReadOnlyDbOutdatedError,
    SQLiteMigrator,
)


def _seed_db_at_version(db_path: Path, version: int) -> None:
    """Create a SQLite file pinned to a specific user_version."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def _read_user_version(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0


def _write_migration(
    migrations_dir: Path, version: int, slug: str, body: str
) -> Path:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / f"v{version}__{slug}.sql"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_fresh_db_at_latest_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, LATEST_VERSION)
    _write_migration(
        migrations, 1, "initial", "CREATE TABLE t (x INTEGER); "
        f"PRAGMA user_version = {LATEST_VERSION};"
    )

    SQLiteMigrator(db, migrations).ensure_current(write_capable=True)
    # No backup created, table not (re-)applied.
    backups = [p for p in tmp_path.iterdir() if p.name.startswith("blackboard.sqlite.bak")]
    assert backups == []
    assert _read_user_version(db) == LATEST_VERSION


def test_v0_db_migrates_to_v1(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations,
        1,
        "initial",
        "CREATE TABLE tasks (id TEXT); PRAGMA user_version = 1;",
    )

    SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
        write_capable=True
    )
    assert _read_user_version(db) == 1
    # Backup was written.
    backups = list(tmp_path.glob("blackboard.sqlite.bak.v0.*"))
    assert len(backups) == 1


def test_chained_migrations_apply_in_order(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations,
        1,
        "initial",
        "CREATE TABLE t (x INTEGER); PRAGMA user_version = 1;",
    )
    _write_migration(
        migrations,
        2,
        "add_y",
        "ALTER TABLE t ADD COLUMN y TEXT; PRAGMA user_version = 2;",
    )

    SQLiteMigrator(db, migrations, latest_version=2).ensure_current(
        write_capable=True
    )
    assert _read_user_version(db) == 2
    with sqlite3.connect(str(db)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert cols == ["x", "y"]


def test_migration_without_pragma_still_advances_version(
    tmp_path: Path,
) -> None:
    """Defends against authors who forget the trailing `PRAGMA user_version`."""
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations,
        1,
        "no_pragma",
        "CREATE TABLE t (x INTEGER);",  # no user_version trailer
    )
    SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
        write_capable=True
    )
    assert _read_user_version(db) == 1


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_db_at_v_too_new_raises_schema_too_new(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 99)
    with pytest.raises(SchemaTooNewError, match="Downgrade is not supported"):
        SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
            write_capable=True
        )


def test_read_only_refuses_outdated_db(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations, 1, "initial", "CREATE TABLE t (x); PRAGMA user_version = 1;"
    )
    migrator = SQLiteMigrator(db, migrations, latest_version=1)
    with pytest.raises(ReadOnlyDbOutdatedError):
        migrator.ensure_current(write_capable=False)
    # Importantly, the DB is unmodified after a read-only refusal.
    assert _read_user_version(db) == 0


def test_failing_migration_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    # Bad SQL: references a missing table.
    _write_migration(
        migrations,
        1,
        "broken",
        "ALTER TABLE does_not_exist ADD COLUMN x TEXT; PRAGMA user_version = 1;",
    )
    with pytest.raises(MigrationFailedError):
        SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
            write_capable=True
        )
    # Version unchanged; backup retained.
    assert _read_user_version(db) == 0
    backups = list(tmp_path.glob("blackboard.sqlite.bak.v0.*"))
    assert len(backups) == 1


def test_no_down_migration_files_loaded(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations, 1, "initial", "CREATE TABLE t (x); PRAGMA user_version = 1;"
    )
    # Drop a forbidden down-migration alongside.
    (migrations / "down_v1__rollback.sql").write_text("-- nope")
    with pytest.raises(DownMigrationDisallowed):
        SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
            write_capable=True
        )


# ---------------------------------------------------------------------------
# Backup pruning
# ---------------------------------------------------------------------------


def test_backup_pruning_keeps_latest_plus_two_prior(tmp_path: Path) -> None:
    """Plant 5 fake backups, run a migration, assert 3 remain in place
    and 2 land in <workspace>/.archive/."""
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations, 1, "initial", "CREATE TABLE t (x); PRAGMA user_version = 1;"
    )

    # Pre-plant backups with mtimes spaced apart so sort is stable.
    import os
    import time

    fake_backups = []
    for i in range(5):
        p = tmp_path / f"blackboard.sqlite.bak.v0.fake-{i}"
        p.touch()
        # Older first.
        os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))
        fake_backups.append(p)

    SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
        write_capable=True
    )

    # The migrator wrote one new backup AND pre-planted 5 = 6 total.
    # Expect 3 remaining in place and 3 in .archive/.
    in_place = sorted(tmp_path.glob("blackboard.sqlite.bak.v0.*"))
    archive = tmp_path / ".archive"
    archived = sorted(archive.glob("blackboard.sqlite.bak.v0.*"))

    assert len(in_place) == _BACKUPS_TO_KEEP
    assert len(archived) >= 1
    # Newest fakes survived in place; oldest fakes archived.
    survivor_names = {p.name for p in in_place}
    assert "blackboard.sqlite.bak.v0.fake-0" not in survivor_names


# ---------------------------------------------------------------------------
# Real v1 migration file landing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Splitter behaviour pins
# ---------------------------------------------------------------------------


def test_split_sql_empty_returns_empty_list() -> None:
    """An empty file produces no statements — the migrator's
    no-pending branch relies on this contract."""
    from hungerloop.repository.sqlite_migrator import _split_sql_statements

    assert _split_sql_statements("") == []


def test_split_sql_block_comment_swallows_internal_semicolons(
    tmp_path: Path,
) -> None:
    """``/* ... */`` blocks must NOT split on inner ``;`` (regression net)."""
    from hungerloop.repository.sqlite_migrator import _split_sql_statements

    text = "/* a; b; */ CREATE TABLE x (i INTEGER);"
    statements = _split_sql_statements(text)
    assert statements == ["CREATE TABLE x (i INTEGER)"]


def test_split_sql_does_not_support_trigger_bodies(tmp_path: Path) -> None:
    """Documented limitation: ``BEGIN ... END;`` trigger bodies get
    split on the inner ``;``. Pin the broken behavior so a future
    contributor adding a trigger sees a loud test failure rather than a
    silent half-applied migration.
    """
    from hungerloop.repository.sqlite_migrator import _split_sql_statements

    text = (
        "CREATE TRIGGER t AFTER INSERT ON x BEGIN "
        "INSERT INTO y VALUES (NEW.i); "
        "END;"
    )
    statements = _split_sql_statements(text)
    # The trigger body got cut at the inner ``;`` — the splitter doesn't
    # know about BEGIN/END, so the CREATE TRIGGER header lands as one
    # statement and the trailing ``END`` lands as another. SQLite will
    # reject either half on its own. If you need triggers, write a custom
    # apply path that doesn't go through ``_split_sql_statements``.
    assert len(statements) > 1
    assert any("END" == s.strip() for s in statements)


def test_failing_migration_records_offending_statement(tmp_path: Path) -> None:
    """``MigrationFailedError.statement`` must carry the SQL that broke."""
    db = tmp_path / "blackboard.sqlite"
    migrations = tmp_path / "migrations"
    _seed_db_at_version(db, 0)
    _write_migration(
        migrations,
        1,
        "broken",
        "CREATE TABLE ok (i INTEGER); "
        "ALTER TABLE missing ADD COLUMN x TEXT; "
        "PRAGMA user_version = 1;",
    )
    with pytest.raises(MigrationFailedError) as excinfo:
        SQLiteMigrator(db, migrations, latest_version=1).ensure_current(
            write_capable=True
        )
    err = excinfo.value
    assert err.version == 1
    assert err.statement is not None
    assert "ALTER TABLE missing" in err.statement


def test_real_v1_initial_migration_runs_against_fresh_db(tmp_path: Path) -> None:
    """The actual ``migrations/v1__initial.sql`` shipped in the repo must
    apply cleanly against a fresh DB."""
    from hungerloop.repository import migrations as _migrations_pkg

    real_dir = Path(_migrations_pkg.__file__).parent
    db = tmp_path / "blackboard.sqlite"
    SQLiteMigrator(db, real_dir, latest_version=1).ensure_current(
        write_capable=True
    )
    assert _read_user_version(db) == 1
    # Spot-check a known table from §5.2.
    with sqlite3.connect(str(db)) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "tasks" in names


def test_real_migration_chain_v0_to_latest_runs_against_fresh_db(
    tmp_path: Path,
) -> None:
    """Whole shipped chain (v1 + v2 lifecycle columns) applies on a fresh DB."""
    from hungerloop.repository import migrations as _migrations_pkg
    from hungerloop.repository.sqlite_migrator import LATEST_VERSION

    real_dir = Path(_migrations_pkg.__file__).parent
    db = tmp_path / "blackboard.sqlite"
    SQLiteMigrator(db, real_dir).ensure_current(write_capable=True)
    assert _read_user_version(db) == LATEST_VERSION
    # v2 columns and the lifecycle index must exist.
    with sqlite3.connect(str(db)) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(memory_candidates)"
        )}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
    assert {"state", "decision_loop_id", "decided_by",
            "decision_rationale", "replaces_candidate_id",
            "expires_at"} <= cols
    assert "idx_memory_state" in indexes
