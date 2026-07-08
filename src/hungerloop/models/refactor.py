"""RefactorTransaction model for v0.7 bounded non-monotonic commit windows.

A ``RefactorTransaction`` represents a bounded exception to invariant I-3
(check-level commits only). While a transaction is open, declared regression
keys may be tolerated by ``CommitManager`` until settlement (ADR-010).

Lifecycle:
    1. ``open``      - created by ``RefactorTransactionManager.open``.
    2. ``closed_success`` - declared regressions recovered with net progress.
    3. ``rolled_back``    - settlement failed; best state rolled back to baseline.

This model is a data container only. Business behavior lives in services.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hungerloop.models.blackboard import BestState


class RefactorTransactionStatus(str, Enum):
    """Lifecycle status for a refactor transaction."""

    OPEN = "open"
    CLOSED_SUCCESS = "closed_success"
    ROLLED_BACK = "rolled_back"


def _is_safe_relative_path(path: str) -> bool:
    """Check that ``path`` is a relative path without traversal.

    Rejects:
      - Empty strings
      - Absolute paths (POSIX ``/`` or Windows drive-letter ``C:\\``)
      - Path traversal (``..`` components)
      - NUL bytes
    """
    if not path or not path.strip():
        return False
    if "\x00" in path:
        return False
    # Check for Windows drive letter absolute paths
    if len(path) >= 2 and path[1] == ":":
        return False
    # Check for backslash-absolute Windows paths
    if path.startswith("\\\\"):
        return False
    # Use PurePosixPath for consistent checking (forward slashes)
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    # Check for path traversal
    parts = normalized.split("/")
    if ".." in parts:
        return False
    return True


class RefactorTransaction(BaseModel):
    """A bounded refactor transaction with declared regression tolerance.

    Fields:
        transaction_id: Unique identifier for this transaction.
        task_id: The task this transaction belongs to.
        opening_loop: The loop number when the transaction was opened.
        deadline_loop: The loop by which the transaction must settle
            (``opening_loop + policy.refactor_deadline_loops``).
        declared_regression_keys: Non-empty, unique check keys that may
            regress while the transaction is open.
        baseline_accepted_check_keys: Accepted check keys at open time.
        baseline_accepted_check_count: Count of baseline accepted checks
            (must match ``len(baseline_accepted_check_keys)``).
        baseline_best_state: The BestState snapshot at open time. Its
            ``task_id`` must match this transaction's ``task_id``.
        snapshot_path: Safe relative path to the best-files snapshot
            directory.
        status: Current lifecycle status.
        closed_loop: The loop when the transaction was closed (None while open).
        close_reason: Human-readable reason for closure (None while open).
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    task_id: str
    opening_loop: int
    deadline_loop: int
    declared_regression_keys: list[str] = Field(min_length=1)
    baseline_accepted_check_keys: list[str] = Field(min_length=1)
    baseline_accepted_check_count: int
    baseline_best_state: BestState
    snapshot_path: str
    status: RefactorTransactionStatus = RefactorTransactionStatus.OPEN
    closed_loop: int | None = None
    close_reason: str | None = None

    # -----------------------------------------------------------------
    # Field validators
    # -----------------------------------------------------------------

    @field_validator("transaction_id")
    @classmethod
    def _validate_transaction_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("transaction_id must be a non-empty string")
        return v

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("task_id must be a non-empty string")
        return v

    @field_validator("opening_loop")
    @classmethod
    def _validate_opening_loop(cls, v: int) -> int:
        if v < 0:
            raise ValueError("opening_loop must not be negative")
        return v

    @field_validator("deadline_loop")
    @classmethod
    def _validate_deadline_loop(cls, v: int) -> int:
        if v < 0:
            raise ValueError("deadline_loop must not be negative")
        return v

    @field_validator("declared_regression_keys")
    @classmethod
    def _validate_declared_regression_keys(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("declared_regression_keys must not be empty")
        if len(v) != len(set(v)):
            raise ValueError("declared_regression_keys must not contain duplicates")
        return v

    @field_validator("baseline_accepted_check_keys")
    @classmethod
    def _validate_baseline_keys(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("baseline_accepted_check_keys must not be empty")
        return v

    @field_validator("snapshot_path")
    @classmethod
    def _validate_snapshot_path(cls, v: str) -> str:
        if not _is_safe_relative_path(v):
            raise ValueError(
                "snapshot_path must be a safe relative path "
                "(no absolute paths, traversal, or NUL bytes)"
            )
        return v

    # -----------------------------------------------------------------
    # Model validator (cross-field)
    # -----------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_cross_field_constraints(self) -> RefactorTransaction:
        # Deadline must be strictly after opening loop
        if self.deadline_loop <= self.opening_loop:
            raise ValueError(
                "deadline_loop must be strictly greater than opening_loop"
            )

        # Baseline count must match keys length
        if self.baseline_accepted_check_count != len(self.baseline_accepted_check_keys):
            raise ValueError(
                "baseline_accepted_check_count must match "
                "len(baseline_accepted_check_keys)"
            )

        # Baseline best state task_id must match transaction task_id
        if self.baseline_best_state.task_id != self.task_id:
            raise ValueError(
                "baseline_best_state.task_id must match transaction task_id"
            )

        return self
