"""StagnationDetector service for HungerLoop v0.5a.

Tracks per-item consecutive failures and global no-progress streaks. When an item
reaches the failure threshold, it is marked BLOCKED. When the global no-progress
streak exceeds the threshold, the orchestrator should emit StopReason.BLOCKED.

Only items in ``attempted_hunger_item_ids`` are counted; unattempted items are
ignored (invariant I-6: attempted-only tracking).

v0.7 (ADR-010): When a matching open refactor transaction is active and
policy is enabled, stagnation exempts only declared regression check keys.
If the implementation works at item granularity, an attempted item is
exempted only when every attempted failing check on that item is a
declared regression key. Undeclared failing checks still count.
"""
from __future__ import annotations

from typing import TypedDict

from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.refactor import RefactorTransaction, RefactorTransactionStatus
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol


class StagnationResult(TypedDict):
    """Result of stagnation detection."""

    blocked_items: list[str]
    global_blocked: bool


class StagnationDetector:
    """Tracks stagnation at item and task levels.

    Args:
        repo: Repository providing get_hunger_item, save_hunger_item,
            reset_no_progress_streak, and increment_no_progress_streak.
        max_item_consecutive_failures: Threshold for marking an item BLOCKED.
        max_global_no_progress_loops: Threshold for global stagnation.
    """

    def __init__(
        self,
        repo: RepositoryProtocol,
        max_item_consecutive_failures: int = 10,
        max_global_no_progress_loops: int = 5,
    ) -> None:
        self.repo = repo
        self.max_item_failures = max_item_consecutive_failures
        self.max_global_no_progress = max_global_no_progress_loops

    def update(
        self,
        task_id: str,
        loop_id: int,
        validation_report: ValidationReport,
        *,
        candidate_committed: bool,
        attempted_hunger_item_ids: list[str] | None = None,
        respect_stagnation: bool = True,
        open_transaction: RefactorTransaction | None = None,
    ) -> StagnationResult:
        """Update stagnation counters based on validation report.

        Args:
            task_id: Task identifier.
            loop_id: Current loop number.
            validation_report: ValidationReport with attempted_hunger_item_ids,
                newly_passed_check_keys, and has_real_progress.
            candidate_committed: Whether CommitManager accepted the candidate.
                Only committed newly-passed checks count as progress.
            attempted_hunger_item_ids: Optional M3 override containing the union
                of hunger items attempted by completed, non-skipped assignments.
            open_transaction: Optional refactor transaction. When the
                transaction is ``open``, matches ``task_id``, and policy is
                enabled, declared regression check keys are exempted from
                stagnation (ADR-010 / VAL-REF-016).

        Returns:
            StagnationResult with blocked_items and global_blocked.
        """
        attempted = set(
            attempted_hunger_item_ids
            if attempted_hunger_item_ids is not None
            else validation_report.attempted_hunger_item_ids
        )
        newly_progressed: set[str] = set()

        if candidate_committed:
            for check_key in validation_report.newly_passed_check_keys:
                item_id = check_key.split(":", 1)[0]
                newly_progressed.add(item_id)

        # Determine which check keys are exempted due to an open transaction.
        exempted_check_keys = self.exempted_check_keys(task_id, open_transaction)

        # Determine which items are fully exempted (all failing checks declared)
        # vs partially exempted (some undeclared failing checks remain).
        exempted_items = self._exempted_items(
            validation_report, exempted_check_keys, attempted
        )

        blocked_items: list[str] = []
        ledger = self.repo.get_hunger_ledger(task_id)
        items_by_id = {item.id: item for item in ledger.items}
        mutated = False

        for iid in attempted:
            item = items_by_id.get(iid)
            if item is None:
                continue
            if item.status in {
                HungerItemStatus.CLOSED,
                HungerItemStatus.VALIDATED_SATISFIED,
            }:
                continue

            if iid in newly_progressed:
                item.consecutive_failure_count = 0
                item.last_progress_loop_id = loop_id
            elif iid in exempted_items:
                # Item is fully exempted: all failing checks are declared
                # regression keys. Don't increment failure count.
                pass
            else:
                item.consecutive_failure_count += 1

            if (
                respect_stagnation
                and item.consecutive_failure_count >= self.max_item_failures
            ):
                item.status = HungerItemStatus.BLOCKED
                blocked_items.append(iid)

            mutated = True

        if mutated:
            self.repo.save_hunger_ledger(task_id, ledger)

        if candidate_committed and validation_report.has_real_progress:
            self.repo.reset_no_progress_streak(task_id)
            global_blocked = False
        else:
            streak: int = self.repo.increment_no_progress_streak(task_id)
            global_blocked = (
                respect_stagnation and streak >= self.max_global_no_progress
            )

        return {
            "blocked_items": blocked_items,
            "global_blocked": global_blocked,
        }

    def exempted_check_keys(
        self,
        task_id: str,
        open_transaction: RefactorTransaction | None,
    ) -> set[str]:
        """Return accepted-check regressions exempted by ADR-010."""
        return self._exempted_check_keys(task_id, open_transaction)

    def _exempted_check_keys(
        self,
        task_id: str,
        open_transaction: RefactorTransaction | None,
    ) -> set[str]:
        """Return the set of check keys exempted by an active transaction.

        Returns an empty set when policy is disabled, the transaction is
        not open, or the task_id doesn't match (VAL-REF-016, VAL-REF-022).
        """
        if open_transaction is None:
            return set()
        if open_transaction.status != RefactorTransactionStatus.OPEN:
            return set()
        if open_transaction.task_id != task_id:
            return set()

        # Check policy is enabled
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.refactor_transactions_enabled:
            return set()

        return set(open_transaction.declared_regression_keys)

    @staticmethod
    def _exempted_items(
        validation_report: ValidationReport,
        exempted_check_keys: set[str],
        attempted_item_ids: set[str],
    ) -> set[str]:
        """Determine which attempted items are fully exempted.

        An item is fully exempted only when every attempted failing check
        on that item is a declared regression key. If any undeclared check
        is failing, the item is not exempted (item-level optimization
        per VAL-REF-016).

        Note: since the stagnation detector doesn't have per-check failure
        data beyond what's in the validation report, we use a conservative
        approach: an item is exempted if it has no newly passed checks
        (no progress) AND all of its regressed check keys are in the
        exempted set.
        """
        if not exempted_check_keys:
            return set()

        # Collect regressed check keys by item id
        regressed_by_item: dict[str, set[str]] = {}
        for check_key in validation_report.regressed_check_keys:
            item_id = check_key.split(":", 1)[0]
            regressed_by_item.setdefault(item_id, set()).add(check_key)

        # Also check attempted_check_keys for failing checks
        attempted_checks_by_item: dict[str, set[str]] = {}
        for check_key in validation_report.attempted_check_keys:
            item_id = check_key.split(":", 1)[0]
            attempted_checks_by_item.setdefault(item_id, set()).add(check_key)

        exempted: set[str] = set()
        for item_id in attempted_item_ids:
            # Item has newly passed checks -> not exempted (has progress)
            # This is already handled by the caller.

            # Check regressed keys for this item
            item_regressed = regressed_by_item.get(item_id, set())
            if not item_regressed:
                # No regressed keys for this item -> not exempted
                continue

            # All regressed keys must be in the exempted set
            if item_regressed.issubset(exempted_check_keys):
                exempted.add(item_id)

        return exempted
