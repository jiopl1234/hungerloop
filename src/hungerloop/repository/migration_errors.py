"""Migration error types for the SQLite forward-only migrator (PRD §5.5)."""
from __future__ import annotations


class MigrationError(RuntimeError):
    """Base class for all migrator failures."""


class SchemaTooNewError(MigrationError):
    """Raised when the on-disk DB ``user_version`` exceeds ``LATEST_VERSION``.

    Downgrade is not supported (PRD §5.5: "Migrations are forward-only").
    Operators must use the corresponding code version that produced the DB.
    """


class MigrationFailedError(MigrationError):
    """Raised when a migration file fails inside its ``BEGIN IMMEDIATE``
    transaction. The transaction rolls back; the sibling backup is
    preserved for forensic recovery.

    ``statement`` carries the SQL statement that failed (truncated to
    keep error messages readable). It is ``None`` for failures that
    happen outside the per-statement loop (e.g. the trailing
    ``PRAGMA user_version`` write).
    """

    _STATEMENT_PREVIEW_CHARS = 240

    def __init__(
        self,
        *,
        version: int,
        cause: BaseException,
        statement: str | None = None,
    ) -> None:
        preview = _preview(statement, self._STATEMENT_PREVIEW_CHARS)
        suffix = f" (failing statement: {preview!r})" if preview else ""
        super().__init__(
            f"Migration v{version} failed; backup preserved.{suffix} "
            f"Underlying cause: {cause!r}"
        )
        self.version = version
        self.cause = cause
        self.statement = statement


def _preview(text: str | None, limit: int) -> str | None:
    """Return ``text`` collapsed to a single line and trimmed to ``limit``.

    Used for log lines / error messages so a 200-line CREATE TABLE doesn't
    blow up the operator's terminal.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class DownMigrationDisallowed(MigrationError):
    """Raised when a ``down_v{N}*.sql`` file is found under ``migrations/``.

    Forward-only is a hard rule; presence of a down-migration file is a
    "code/repo mismatch" signal, not a feature.
    """


class IllegalPhaseTransition(MigrationError):
    """Raised when mission phase or feature status moves through a forbidden edge."""

    def __init__(
        self,
        message: str,
        *,
        phase_id: str | None = None,
        feature_id: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.phase_id = phase_id
        self.feature_id = feature_id
        self.from_status = from_status
        self.to_status = to_status
