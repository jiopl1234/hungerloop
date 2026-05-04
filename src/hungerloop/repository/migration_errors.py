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
    """

    def __init__(self, *, version: int, cause: BaseException) -> None:
        super().__init__(
            f"Migration v{version} failed; backup preserved. "
            f"Underlying cause: {cause!r}"
        )
        self.version = version
        self.cause = cause


class DownMigrationDisallowed(MigrationError):
    """Raised when a ``down_v{N}*.sql`` file is found under ``migrations/``.

    Forward-only is a hard rule; presence of a down-migration file is a
    "code/repo mismatch" signal, not a feature.
    """
