# b0-02 · SQLite migration framework

**Spec**: §2. **PRD**: §5.5. **Release**: v0.5b.0.

## Goal

Forward-only migrations with `PRAGMA user_version` + sibling backups + read-only safe-refusal. Lets every later schema change land cleanly.

## Files to touch

- **NEW** `src/hungerloop/repository/migrations/__init__.py` (empty).
- **NEW** `src/hungerloop/repository/migrations/v1__initial.sql` — current `sqlite_schema.sql` content, plus a final `PRAGMA user_version = 1;`.
- **DELETE** `src/hungerloop/repository/sqlite_schema.sql` (replaced by `v1__initial.sql`).
- **NEW** `src/hungerloop/repository/sqlite_migrator.py` — version detection, backup, apply.
- **NEW** `src/hungerloop/repository/migration_errors.py` — `SchemaTooNewError`, `MigrationFailedError`.
- `src/hungerloop/repository/sqlite_repo.py` (will be created in a parallel task — wire migrator into its `__init__`).
- **NEW** `tests/unit/test_sqlite_migrator.py`.

## Checklist

### Migration directory contract

- [ ] File naming: `v{N}__{slug}.sql`, `N` is a positive integer, slug is `[a-z0-9_]+`.
- [ ] Each file is one transaction; the migrator wraps it in `BEGIN IMMEDIATE` / `COMMIT`.
- [ ] Every migration file ends with `PRAGMA user_version = N;`.
- [ ] Migrations are forward-only — no down-migration files allowed; the migrator refuses to load any file matching `down_v{N}*.sql`.

### Migrator (`sqlite_migrator.py`)

- [ ] `LATEST_VERSION: int = 1` module constant. (Bumped per migration.)
- [ ] `class SQLiteMigrator: __init__(self, db_path: Path, migrations_dir: Path)`.
- [ ] `migrator.ensure_current(write_capable: bool) -> None`:
  - Open DB, read `PRAGMA user_version`.
  - If `current == LATEST_VERSION`: return.
  - If `current > LATEST_VERSION`: raise `SchemaTooNewError`.
  - If `current < LATEST_VERSION` and `write_capable=False`: raise click-friendly error → CLI exits code 4.
  - If `current < LATEST_VERSION` and `write_capable=True`: write backup, apply each pending migration in order, prune old backups (latest + 2 prior).
- [ ] `_write_backup(db_path, current_version) -> Path`: writes `<db>.bak.v{current_version}.{utc_iso8601}`.
- [ ] `_prune_backups(db_path, keep=3) -> None`: lists `*.bak.v*` siblings, sorts by mtime, moves all but the newest 3 to `<workspace>/.archive/` (creating it if needed).
- [ ] On any per-migration failure: SQLite rolls back automatically (transaction); migrator preserves the backup, raises `MigrationFailedError(version=N, cause=...)`.

### Read-only command guard

- [ ] Touch `cli/status_cmd.py`, `cli/report_cmd.py`, `cli/trace_cmd.py` (when it lands), `cli/checks_cmd.py` to call `SQLiteMigrator(...).ensure_current(write_capable=False)` before opening the repo.
  - For v0.5b.0 InMemory tests this is bypassed via the existing `CliContext` injection — only the SQLite production path runs the migrator.

### Tests (`test_sqlite_migrator.py`)

- [ ] `test_fresh_db_at_latest_is_noop` — open a fresh `:memory:` style DB at v1, ensure no backup, no rewrite.
- [ ] `test_v0_db_migrates_to_v1` — synthesize a "pre-versioned" empty DB, run migrator, assert `user_version=1` + backup file exists.
- [ ] `test_db_at_v_too_new_raises_schema_too_new` — set `user_version=99`, expect `SchemaTooNewError`.
- [ ] `test_read_only_refuses_outdated_db` — `ensure_current(write_capable=False)` on v0 DB exits with click error.
- [ ] `test_failing_migration_rolls_back_and_keeps_backup` — synthesize a v2 migration with a bad SQL, ensure user_version stays at 1 and backup remains.
- [ ] `test_backup_pruning_keeps_latest_plus_two_prior` — create 5 fake backups, run prune, assert 3 remain in place + 2 moved to `.archive/`.
- [ ] `test_no_down_migration_files_loaded` — drop a `down_v1__rollback.sql` next to migrations, assert migrator ignores or refuses (your choice — pick refuse).

## Done when

- [ ] All 7 tests pass.
- [ ] `LATEST_VERSION = 1` and `migrations/v1__initial.sql` is the only file present.
- [ ] `mypy --strict` clean.
- [ ] PRD §5.5 references the migrator.

## Notes

- The actual `SQLiteRepository` class is a separate task; this PR ships the migrator and the v1 file only. SQLite repo wires this into its open path.
- Backup naming uses UTC ISO8601 (no `:` — POSIX-safe variant: `2026-05-04T13-22-09Z`).
