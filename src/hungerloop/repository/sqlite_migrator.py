"""Forward-only SQLite migrator (PRD §5.5).

Lifecycle on every SQLiteRepository open:

    1. read PRAGMA user_version (the source-of-truth for schema version)
    2. if ``current > LATEST_VERSION``           → ``SchemaTooNewError``
    3. if ``current == LATEST_VERSION``           → no-op
    4. if ``current < LATEST_VERSION`` and write_capable:
           write sibling backup, apply each pending migration in order,
           prune backups (keep latest + 2 prior, archive older).
    5. if ``current < LATEST_VERSION`` and read-only:
           refuse with a click-friendly error; caller exits 4.

Each migration file is one ``BEGIN IMMEDIATE`` transaction. Failures roll
back automatically; the backup remains so an operator can restore. There
are no down-migration files — to revert, restore from the backup.

The framework is decoupled from ``SQLiteRepository`` so unit tests can
exercise migration behavior without instantiating the full repo.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from hungerloop.repository.migration_errors import (
    DownMigrationDisallowed,
    MigrationFailedError,
    SchemaTooNewError,
)

# Bump per migration. v0.5b.0 shipped at 1; v0.5c.0 shipped at 2 with
# `migrations/v2__memory_candidate_lifecycle.sql`; v3 adds the runtime
# tables/constraints needed by SQLiteRepository; v4 adds source links for
# MemoryCandidate predicates.
LATEST_VERSION: int = 4

# Migration files: ``v{N}__{slug}.sql`` where N is a positive int and
# slug is ``[a-z0-9_]+``. Anything else is rejected at startup.
_MIGRATION_RE = re.compile(r"^v(?P<n>\d+)__(?P<slug>[a-z0-9_]+)\.sql$")
_DOWN_MIGRATION_RE = re.compile(r"^down_v\d+.*\.sql$")

# Backup-pruning policy: keep the latest backup plus 2 prior versions in
# place; older ones move to ``<workspace>/.archive/``. Backups are a few
# MB each and corruption recovery without one is operator-grade pain
# (PRD §5.5 retention rule).
_BACKUPS_TO_KEEP = 3


class SQLiteMigrator:
    """Versioned SQLite migrator scoped to a single DB file."""

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        *,
        latest_version: int = LATEST_VERSION,
    ) -> None:
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.latest_version = latest_version

    # -----------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------
    def ensure_current(self, *, write_capable: bool) -> None:
        """Bring the DB to ``latest_version`` or refuse politely.

        Args:
            write_capable: True when the calling command is allowed to
                mutate state (``run`` / ``new`` / ``hunger refill`` /
                ``repair-state --fix``). False for read-only commands
                (``status`` / ``report`` / ``trace`` / ``repair-state
                --check``); in that case an outdated DB causes a
                friendly refusal so the user runs a write command first.

        Raises:
            SchemaTooNewError: ``user_version > latest_version``.
            ReadOnlyDbOutdatedError: read-only command on an outdated DB.
            MigrationFailedError: any migration's transaction failed.
            DownMigrationDisallowed: a ``down_v*.sql`` file is present.
        """
        # Forward-only invariant — fail fast if someone dropped a
        # down-migration into the directory.
        self._reject_down_migrations()

        current = self._read_user_version()
        if current > self.latest_version:
            raise SchemaTooNewError(
                f"Database at v{current}; code at v{self.latest_version}. "
                "Downgrade is not supported — restore from backup or "
                "upgrade the codebase."
            )
        if current == self.latest_version:
            return

        if not write_capable:
            raise ReadOnlyDbOutdatedError(
                f"Database at v{current}; code at v{self.latest_version}. "
                "Run 'hungerloop run' (or any write-capable command) once "
                "to apply pending migrations."
            )

        self._migrate_to_latest(current)

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _read_user_version(self) -> int:
        if not self.db_path.exists():
            # A fresh DB starts at user_version=0 by SQLite default.
            return 0
        # ``sqlite3.Connection.__exit__`` only commits / rolls back; it does
        # NOT close the connection. Across N migrations + per-process
        # ensure_current calls that's a real file-handle leak — wrap every
        # open in ``closing(...)``.
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            cur = conn.execute("PRAGMA user_version")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def _reject_down_migrations(self) -> None:
        if not self.migrations_dir.exists():
            return
        for entry in self.migrations_dir.iterdir():
            if entry.is_file() and _DOWN_MIGRATION_RE.match(entry.name):
                raise DownMigrationDisallowed(
                    f"Found {entry.name} under {self.migrations_dir}; "
                    "down-migrations are not supported. "
                    "Remove the file or restore from a backup."
                )

    def _list_pending_migrations(
        self, current_version: int
    ) -> list[tuple[int, Path]]:
        """Return ``(version, file)`` pairs strictly above ``current_version``."""
        pending: list[tuple[int, Path]] = []
        if not self.migrations_dir.exists():
            return pending
        for entry in sorted(self.migrations_dir.iterdir()):
            if not entry.is_file():
                continue
            match = _MIGRATION_RE.match(entry.name)
            if not match:
                # Ignore non-migration siblings (e.g. __init__.py, READMEs).
                continue
            version = int(match.group("n"))
            if version > current_version and version <= self.latest_version:
                pending.append((version, entry))
        pending.sort(key=lambda pair: pair[0])
        return pending

    def _migrate_to_latest(self, current_version: int) -> None:
        pending = self._list_pending_migrations(current_version)
        if not pending:
            raise MigrationFailedError(
                version=self.latest_version,
                cause=RuntimeError(
                    "No migration files found from "
                    f"v{current_version + 1} to v{self.latest_version}"
                ),
            )

        backup_path = self._write_backup(current_version)
        try:
            for version, sql_file in pending:
                self._apply_one(version, sql_file)
        finally:
            self._prune_backups()
        # Backup retained on success too — ops will prune it on the next
        # migration cycle.
        del backup_path

    def _apply_one(self, version: int, sql_file: Path) -> None:
        """Apply one migration file inside a single transaction.

        We can't use ``executescript`` here: Python's sqlite3 issues an
        implicit COMMIT before running the script, which would defeat the
        atomic-migration guarantee. Instead we split the file into
        individual statements and execute each via ``cursor.execute`` so
        the manual ``BEGIN IMMEDIATE`` / ``COMMIT`` actually wraps them.

        Failure carries the offending statement (truncated) into
        :class:`MigrationFailedError` so an operator reading the log
        knows which line broke without having to grep the file.
        """
        statements = _split_sql_statements(
            sql_file.read_text(encoding="utf-8")
        )
        failing_stmt: str | None = None
        try:
            with closing(
                sqlite3.connect(str(self.db_path), isolation_level=None)
            ) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in statements:
                        failing_stmt = stmt
                        conn.execute(stmt)
                    failing_stmt = None
                    # We trust the migration to set user_version itself
                    # (the convention in v1__initial.sql), but also defend
                    # against bad migrations that forget the trailer.
                    cur = conn.execute("PRAGMA user_version")
                    row = cur.fetchone()
                    actual = int(row[0]) if row else 0
                    if actual != version:
                        conn.execute(f"PRAGMA user_version = {version}")
                    conn.execute("COMMIT")
                except BaseException:
                    # If ROLLBACK itself fails (e.g. concurrent writer
                    # holding the DB), prefer the original error — that's
                    # what tells the operator what actually broke.
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
        except sqlite3.Error as exc:
            raise MigrationFailedError(
                version=version, cause=exc, statement=failing_stmt
            ) from exc

    def _set_user_version(self, version: int) -> None:
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()

    def _write_backup(self, current_version: int) -> Path:
        """Sibling backup at ``<db>.bak.v{N}.{utc_iso8601}``.

        Filenames use a POSIX-safe ISO8601 variant (no ``:``).
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        backup = self.db_path.with_name(
            f"{self.db_path.name}.bak.v{current_version}.{ts}"
        )
        if self.db_path.exists():
            shutil.copy2(self.db_path, backup)
        else:
            # Empty placeholder so prune logic always finds a target.
            backup.touch()
        return backup

    def _prune_backups(self) -> None:
        """Keep the newest ``_BACKUPS_TO_KEEP`` backups in place; move
        older ones to ``<workspace>/.archive/``."""
        siblings = sorted(
            (
                p
                for p in self.db_path.parent.iterdir()
                if p.is_file() and p.name.startswith(self.db_path.name + ".bak.v")
            ),
            key=lambda p: p.stat().st_mtime,
        )
        if len(siblings) <= _BACKUPS_TO_KEEP:
            return
        archive = self.db_path.parent / ".archive"
        archive.mkdir(exist_ok=True)
        # Oldest first; move every file beyond the keep window.
        for old in siblings[: -_BACKUPS_TO_KEEP]:
            shutil.move(str(old), str(archive / old.name))


class ReadOnlyDbOutdatedError(RuntimeError):
    """Raised when a read-only command opens a DB with ``user_version <
    LATEST_VERSION``. The CLI catches this and exits with code 4 and a
    "run a write command first" remediation."""


def _split_sql_statements(text: str) -> list[str]:
    """Split a SQL file into top-level statements.

    Handles ``--`` line comments, ``/* ... */`` block comments, and
    respects single-quoted strings so semicolons inside string literals
    don't terminate a statement. We do NOT support BEGIN/END trigger
    bodies or nested compound statements; the splitter would treat the
    inner ``;`` as a statement terminator. None of HungerLoop's
    migrations use triggers — see ``test_split_sql_does_not_support_*``
    for the pinned regression cases.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if not in_string and ch == "/" and text[i : i + 2] == "/*":
            # Block comment runs to the next ``*/``. Unterminated blocks
            # swallow the rest of the file — that's a malformed migration
            # and the operator will see the resulting SQL syntax error.
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        if not in_string and ch == "-" and text[i : i + 2] == "--":
            # Line comment to end of line.
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline + 1
            continue
        if ch == "'":
            # Toggle string state; doubled-up '' inside a string is an
            # escape but doesn't change toggle parity (toggle off then on).
            in_string = not in_string
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_string:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
