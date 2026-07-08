"""RefactorTransactionManager for v0.7 bounded non-monotonic commit windows.

This service implements the lifecycle of a refactor transaction:

1. **Open**: validates policy, single-open state, declared keys, limits,
   snapshots best files, derives deadline from ``opening_loop + policy.
   refactor_deadline_loops``, and persists the transaction + audit event.
2. **Close**: checks whether declared regressions recovered and the current
   accepted-check set is a strict superset of the baseline. On success,
   persists ``closed_success`` atomically. On failure, rolls back best
   files and best state from the snapshot, persists ``rolled_back``,
   and emits the rollback event.
3. **Settle if due**: invoked by the orchestrator each loop to close
   transactions whose deadline has been reached.

Key invariants (VAL-REF-009 through VAL-REF-027):

- Disabled policy prevents all transaction behavior (VAL-REF-022).
- Deadlines are derived and cannot be supplied or extended (VAL-REF-026).
- Snapshots are independent of later best-workspace changes (VAL-REF-010).
- Close success requires strict superset (VAL-REF-011).
- Rollback is atomic and retryable (VAL-REF-012, VAL-REF-013).
- Close-success persistence is atomic and retryable (VAL-REF-027).
- Audit events are stable and non-secret (VAL-REF-024).
- Close idempotency is safe (VAL-REF-023).
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from hungerloop.models.events import EventType
from hungerloop.models.refactor import (
    RefactorTransaction,
    RefactorTransactionStatus,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.workspace_manager import WorkspaceManager


@dataclass
class RefactorTxnResult:
    """Result of a transaction operation."""

    success: bool
    reason: str
    status: RefactorTransactionStatus
    transaction: RefactorTransaction | None = None


class RefactorTransactionManager:
    """Manage refactor transaction lifecycle (open / close / rollback / settle).

    The manager is the sole service that opens, closes, and rolls back
    refactor transactions. It delegates filesystem operations to
    :class:`WorkspaceManager` and persistence to the repository.
    """

    def __init__(
        self,
        repo: RepositoryProtocol,
        workspace_manager: WorkspaceManager,
    ) -> None:
        self.repo = repo
        self.workspace_manager = workspace_manager

    # -----------------------------------------------------------------
    # Open
    # -----------------------------------------------------------------

    def open(
        self,
        *,
        task_id: str,
        loop_id: int,
        declared_regression_keys: list[str],
        rationale: str = "",
    ) -> RefactorTxnResult:
        """Open a refactor transaction.

        Validates:
        - Policy enabled (``refactor_transactions_enabled``)
        - No existing open transaction for this task
        - Declared keys are non-empty and unique
        - Each declared key is currently accepted
        - Declared count within ``max_declared_regressions``
        - Best state and best files are available
        - Snapshot copy succeeds

        Returns a :class:`RefactorTxnResult`. On failure, no transaction
        is persisted and no snapshot is created.
        """
        policy = self.repo.get_hunger_policy(task_id)

        # Policy gate
        if not policy.refactor_transactions_enabled:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="refactor_transactions_disabled",
            )
            return RefactorTxnResult(
                success=False,
                reason="refactor_transactions_disabled",
                status=RefactorTransactionStatus.OPEN,
            )

        # Non-positive deadline window
        if policy.refactor_deadline_loops <= 0:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="non_positive_deadline_window",
            )
            return RefactorTxnResult(
                success=False,
                reason="non_positive_deadline_window",
                status=RefactorTransactionStatus.OPEN,
            )

        # Single-open check
        existing = self.repo.get_open_refactor_transaction(task_id)
        if existing is not None:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="transaction_already_open",
            )
            return RefactorTxnResult(
                success=False,
                reason="transaction_already_open",
                status=RefactorTransactionStatus.OPEN,
            )

        # Declared keys must be non-empty
        if not declared_regression_keys:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="empty_declared_keys",
            )
            return RefactorTxnResult(
                success=False,
                reason="empty_declared_keys",
                status=RefactorTransactionStatus.OPEN,
            )

        # Declared keys must be unique
        if len(declared_regression_keys) != len(set(declared_regression_keys)):
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="duplicate_declared_keys",
            )
            return RefactorTxnResult(
                success=False,
                reason="duplicate_declared_keys",
                status=RefactorTransactionStatus.OPEN,
            )

        # Declared count within limit
        if len(declared_regression_keys) > policy.max_declared_regressions:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="declared_keys_exceed_limit",
            )
            return RefactorTxnResult(
                success=False,
                reason="declared_keys_exceed_limit",
                status=RefactorTransactionStatus.OPEN,
            )

        # Best state must exist
        best = self.repo.get_best_state(task_id)
        if best is None:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="no_best_state",
            )
            return RefactorTxnResult(
                success=False,
                reason="no_best_state",
                status=RefactorTransactionStatus.OPEN,
            )

        # Each declared key must be currently accepted
        accepted_set = set(best.accepted_check_keys)
        unaccepted = [k for k in declared_regression_keys if k not in accepted_set]
        if unaccepted:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="declared_keys_not_accepted",
            )
            return RefactorTxnResult(
                success=False,
                reason="declared_keys_not_accepted",
                status=RefactorTransactionStatus.OPEN,
            )

        # Best files must exist
        self.workspace_manager.ensure_task_workspace(task_id)
        best_dir = self.workspace_manager.best_files_dir(task_id)
        if not best_dir.exists():
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="no_best_files",
            )
            return RefactorTxnResult(
                success=False,
                reason="no_best_files",
                status=RefactorTransactionStatus.OPEN,
            )

        # Derive deadline (VAL-REF-026: derived, never supplied)
        deadline_loop = loop_id + policy.refactor_deadline_loops

        # Create transaction id and snapshot path
        transaction_id = f"RTXN-{task_id}-{loop_id}-{uuid.uuid4().hex[:8]}"
        snapshot_path = f".txn_{transaction_id}"

        # Snapshot best files
        snapshot_dir = self.workspace_manager.task_root(task_id) / snapshot_path
        try:
            self._copy_best_to_snapshot(task_id, snapshot_dir)
        except Exception:
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="snapshot_copy_failed",
            )
            return RefactorTxnResult(
                success=False,
                reason="snapshot_copy_failed",
                status=RefactorTransactionStatus.OPEN,
            )

        # Build transaction
        txn = RefactorTransaction(
            transaction_id=transaction_id,
            task_id=task_id,
            opening_loop=loop_id,
            deadline_loop=deadline_loop,
            declared_regression_keys=list(declared_regression_keys),
            baseline_accepted_check_keys=list(best.accepted_check_keys),
            baseline_accepted_check_count=len(best.accepted_check_keys),
            baseline_best_state=best,
            snapshot_path=snapshot_path,
            status=RefactorTransactionStatus.OPEN,
        )

        # Persist transaction + open event
        try:
            with self.repo.transaction():
                self.repo.save_refactor_transaction(txn)
                self.repo.append_event(
                    EventType.REFACTOR_TXN_OPENED,
                    {
                        "transaction_id": transaction_id,
                        "task_id": task_id,
                        "loop_id": loop_id,
                        "deadline_loop": deadline_loop,
                        "declared_regression_keys": list(declared_regression_keys),
                        "rationale": rationale[:200] if rationale else "",
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
        except Exception:
            # Clean up snapshot on persistence failure
            self._safe_rmtree(snapshot_dir)
            self._emit_rejected_open(
                task_id=task_id,
                loop_id=loop_id,
                declared_keys=declared_regression_keys,
                reason="persistence_failed",
            )
            return RefactorTxnResult(
                success=False,
                reason="persistence_failed",
                status=RefactorTransactionStatus.OPEN,
            )

        return RefactorTxnResult(
            success=True,
            reason="transaction_opened",
            status=RefactorTransactionStatus.OPEN,
            transaction=txn,
        )

    # -----------------------------------------------------------------
    # Close
    # -----------------------------------------------------------------

    def close(
        self,
        *,
        task_id: str,
        loop_id: int,
        force: bool = False,
    ) -> RefactorTxnResult:
        """Close a refactor transaction.

        If the transaction is already closed or rolled back, this is a
        stable no-op (VAL-REF-023).

        Success requires:
        - All declared regression keys are passing again (in current
          accepted checks)
        - Current accepted-check set is a strict superset of the baseline

        On failure, rolls back best files and best state from the snapshot.

        Returns a :class:`RefactorTxnResult`.
        """
        policy = self.repo.get_hunger_policy(task_id)

        # Disabled policy: no-op
        if not policy.refactor_transactions_enabled:
            return RefactorTxnResult(
                success=False,
                reason="refactor_transactions_disabled",
                status=RefactorTransactionStatus.OPEN,
            )

        txn = self.repo.get_open_refactor_transaction(task_id)
        if txn is None:
            # Check if a transaction was already closed or rolled back
            # for idempotency (VAL-REF-023)
            all_txns = self.repo.list_refactor_transactions(task_id)
            if all_txns:
                latest = all_txns[-1]
                if latest.status != RefactorTransactionStatus.OPEN:
                    return RefactorTxnResult(
                        success=True,
                        reason=f"already_{latest.status.value}",
                        status=latest.status,
                        transaction=latest,
                    )
            return RefactorTxnResult(
                success=False,
                reason="no_open_transaction",
                status=RefactorTransactionStatus.OPEN,
            )

        # Idempotency: already closed/rolled-back
        if txn.status != RefactorTransactionStatus.OPEN:
            return RefactorTxnResult(
                success=True,
                reason=f"already_{txn.status.value}",
                status=txn.status,
                transaction=txn,
            )

        # Check close success conditions
        best = self.repo.get_best_state(task_id)
        if best is None:
            # No best state -> rollback
            return self._rollback(txn, loop_id, "no_best_state_at_close")

        current_accepted = set(best.accepted_check_keys)
        baseline_accepted = set(txn.baseline_accepted_check_keys)
        declared_keys = set(txn.declared_regression_keys)

        # All declared keys must be recovered
        declared_recovered = declared_keys.issubset(current_accepted)

        # Current must be a strict superset of baseline
        is_strict_superset = baseline_accepted.issubset(current_accepted) and (
            current_accepted > baseline_accepted
        )

        if declared_recovered and is_strict_superset:
            return self._close_success(txn, loop_id)
        else:
            reason = "close_conditions_not_met"
            if not declared_recovered:
                reason = "declared_keys_not_recovered"
            elif not is_strict_superset:
                reason = "not_strict_superset"
            return self._rollback(txn, loop_id, reason)

    # -----------------------------------------------------------------
    # Settle if due
    # -----------------------------------------------------------------

    def settle_if_due(
        self,
        *,
        task_id: str,
        current_loop: int,
    ) -> RefactorTxnResult | None:
        """Settle a transaction if its deadline has been reached.

        Returns ``None`` if no settlement occurred (no open transaction,
        not yet due, or policy disabled). Returns a
        :class:`RefactorTxnResult` if settlement was attempted.
        """
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.refactor_transactions_enabled:
            return None

        txn = self.repo.get_open_refactor_transaction(task_id)
        if txn is None:
            return None

        if txn.status != RefactorTransactionStatus.OPEN:
            return None

        if current_loop < txn.deadline_loop:
            return None

        return self.close(task_id=task_id, loop_id=current_loop, force=True)

    # -----------------------------------------------------------------
    # Get open transaction (policy-aware)
    # -----------------------------------------------------------------

    def get_active_transaction(self, task_id: str) -> RefactorTransaction | None:
        """Return the open transaction for a task, or None if disabled/no open.

        When policy is disabled, returns ``None`` even if a stale open
        row exists (VAL-REF-022).
        """
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.refactor_transactions_enabled:
            return None
        return self.repo.get_open_refactor_transaction(task_id)

    # -----------------------------------------------------------------
    # Private: close success
    # -----------------------------------------------------------------

    def _close_success(
        self,
        txn: RefactorTransaction,
        loop_id: int,
    ) -> RefactorTxnResult:
        """Persist closed_success atomically, then clean up snapshot."""
        try:
            with self.repo.transaction():
                updated = self.repo.update_refactor_transaction_status(
                    transaction_id=txn.transaction_id,
                    status=RefactorTransactionStatus.CLOSED_SUCCESS,
                    closed_loop=loop_id,
                    close_reason="all_declared_recovered_with_net_progress",
                )
                if updated is None:
                    raise RuntimeError("update_refactor_transaction_status returned None")
                self.repo.append_event(
                    EventType.REFACTOR_TXN_CLOSED_SUCCESS,
                    {
                        "transaction_id": txn.transaction_id,
                        "task_id": txn.task_id,
                        "loop_id": loop_id,
                        "declared_regression_keys": list(txn.declared_regression_keys),
                        "result": "closed_success",
                    },
                    task_id=txn.task_id,
                    loop_id=loop_id,
                )
        except Exception:
            # Persistence failed: transaction stays open/retryable
            self._emit_settle_failed(txn, loop_id, "close_success_persistence_failed")
            return RefactorTxnResult(
                success=False,
                reason="close_success_persistence_failed",
                status=RefactorTransactionStatus.OPEN,
                transaction=txn,
            )

        # Clean up snapshot after successful persistence
        snapshot_dir = self.workspace_manager.task_root(txn.task_id) / txn.snapshot_path
        self._safe_rmtree(snapshot_dir)

        return RefactorTxnResult(
            success=True,
            reason="closed_success",
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            transaction=updated,
        )

    # -----------------------------------------------------------------
    # Private: rollback
    # -----------------------------------------------------------------

    def _rollback(
        self,
        txn: RefactorTransaction,
        loop_id: int,
        reason: str,
    ) -> RefactorTxnResult:
        """Roll back best files and best state from snapshot.

        If restoration fails, the transaction stays open and retryable
        (VAL-REF-013). No rolled_back event is persisted until
        restoration succeeds.
        """
        # Restore best files from snapshot
        try:
            self._restore_best_from_snapshot(txn)
        except Exception:
            # Restoration failed: transaction stays open/retryable
            self._emit_settle_failed(txn, loop_id, "rollback_restoration_failed")
            return RefactorTxnResult(
                success=False,
                reason="rollback_restoration_failed",
                status=RefactorTransactionStatus.OPEN,
                transaction=txn,
            )

        # Restore best state from baseline
        try:
            with self.repo.transaction():
                self.repo.save_best_state(txn.baseline_best_state)
                updated = self.repo.update_refactor_transaction_status(
                    transaction_id=txn.transaction_id,
                    status=RefactorTransactionStatus.ROLLED_BACK,
                    closed_loop=loop_id,
                    close_reason=reason,
                )
                if updated is None:
                    raise RuntimeError("update_refactor_transaction_status returned None")
                self.repo.append_event(
                    EventType.REFACTOR_TXN_ROLLED_BACK,
                    {
                        "transaction_id": txn.transaction_id,
                        "task_id": txn.task_id,
                        "loop_id": loop_id,
                        "declared_regression_keys": list(txn.declared_regression_keys),
                        "reason": reason,
                        "result": "rolled_back",
                    },
                    task_id=txn.task_id,
                    loop_id=loop_id,
                )
        except Exception:
            # Persistence failed: transaction stays open/retryable
            # Best files were already restored, but best state may not be.
            # The snapshot remains available for retry.
            self._emit_settle_failed(txn, loop_id, "rollback_persistence_failed")
            return RefactorTxnResult(
                success=False,
                reason="rollback_persistence_failed",
                status=RefactorTransactionStatus.OPEN,
                transaction=txn,
            )

        # Clean up snapshot after successful rollback
        snapshot_dir = self.workspace_manager.task_root(txn.task_id) / txn.snapshot_path
        self._safe_rmtree(snapshot_dir)

        return RefactorTxnResult(
            success=False,
            reason=reason,
            status=RefactorTransactionStatus.ROLLED_BACK,
            transaction=updated,
        )

    # -----------------------------------------------------------------
    # Private: snapshot filesystem operations
    # -----------------------------------------------------------------

    def _copy_best_to_snapshot(self, task_id: str, snapshot_dir: Path) -> None:
        """Copy best/files into the snapshot directory.

        Excludes previous transaction snapshots (``.txn_*``) and internal
        transaction artifacts.
        """
        best_dir = self.workspace_manager.best_files_dir(task_id)
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        if not best_dir.exists():
            return

        for item in best_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(best_dir)
                # Skip transaction snapshot directories
                if any(part.startswith(".txn_") for part in rel.parts):
                    continue
                dst = snapshot_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(dst))

    def _restore_best_from_snapshot(self, txn: RefactorTransaction) -> None:
        """Restore best/files from the transaction snapshot.

        Restores deleted files, removes transaction-only files, and
        ensures the exact baseline file set is present.
        """
        snapshot_dir = self.workspace_manager.task_root(txn.task_id) / txn.snapshot_path
        best_dir = self.workspace_manager.best_files_dir(txn.task_id)

        if not snapshot_dir.exists():
            raise FileNotFoundError(
                f"Snapshot directory not found: {snapshot_dir}"
            )

        # Remove current best files
        if best_dir.exists():
            shutil.rmtree(best_dir)
        best_dir.mkdir(parents=True, exist_ok=True)

        # Copy snapshot files back to best
        for item in snapshot_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(snapshot_dir)
                dst = best_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(dst))

    # -----------------------------------------------------------------
    # Private: event helpers
    # -----------------------------------------------------------------

    def _emit_rejected_open(
        self,
        *,
        task_id: str,
        loop_id: int,
        declared_keys: list[str],
        reason: str,
    ) -> None:
        """Emit a stable non-secret audit event for a rejected open."""
        try:
            self.repo.append_event(
                EventType.REFACTOR_TXN_OPEN_REJECTED,
                {
                    "task_id": task_id,
                    "loop_id": loop_id,
                    "declared_regression_keys": list(declared_keys),
                    "reason": reason,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        except Exception:
            pass

    def _emit_settle_failed(
        self,
        txn: RefactorTransaction,
        loop_id: int,
        reason: str,
    ) -> None:
        """Emit a settle-failed audit event."""
        try:
            self.repo.append_event(
                EventType.REFACTOR_TXN_SETTLE_FAILED,
                {
                    "transaction_id": txn.transaction_id,
                    "task_id": txn.task_id,
                    "loop_id": loop_id,
                    "reason": reason,
                },
                task_id=txn.task_id,
                loop_id=loop_id,
            )
        except Exception:
            pass

    @staticmethod
    def _safe_rmtree(path: Path) -> None:
        """Best-effort rmtree that never raises."""
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
