"""HungerUpdateService for HungerLoop v0.4.1.

Applies validation outcomes to hunger items by decrementing their ``gap_score``
proportionally to the number of newly-passed acceptance checks (check-level
progress, invariant I-3). When an item reaches zero gap, its status transitions
to VALIDATED_SATISFIED. Otherwise an item with progress becomes WORKING.

Float convergence (PRD §14):
    Multi-round decrement of ``new_count / total_checks`` can leave a residual
    of ~1e-17 so ``gap_score == 0.0`` never holds. The fix:

    * Items present in ``satisfied_hunger_item_ids`` are force-zeroed.
    * All other items snap to 0.0 once ``gap_score <= EPSILON`` (1e-9).

FAIL verdicts are a no-op — no writes occur.

Malformed check keys (no ":" separator) are skipped — a single bad upstream
report must not crash hunger updates for unrelated items.
"""
from __future__ import annotations

import logging

from hungerloop.models.enums import HungerItemStatus, ValidationVerdict
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol

EPSILON = 1e-9

logger = logging.getLogger(__name__)


class HungerUpdateService:
    """Applies validation outcomes to hunger items."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def apply_validation(self, task_id: str, report: ValidationReport) -> None:
        """Apply a ValidationReport to the task's hunger items.

        Args:
            task_id: Task identifier (reserved for repo lookups / logging).
            report: ValidationReport with verdict, newly_passed_check_keys,
                satisfied_hunger_item_ids, evidence_ids, and loop_id.
        """
        if report.verdict not in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}:
            return

        progress_by_item: dict[str, int] = {}
        for key in report.newly_passed_check_keys:
            if ":" not in key:
                logger.warning(
                    "hunger_update: skipping malformed check_key %r in "
                    "report %s (task=%s); expected '<item_id>:<index>'",
                    key,
                    report.id,
                    task_id,
                )
                continue
            item_id, _ = key.split(":", 1)
            progress_by_item[item_id] = progress_by_item.get(item_id, 0) + 1

        ledger = self.repo.get_hunger_ledger(task_id)
        items_by_id = {item.id: item for item in ledger.items}
        mutated = False
        for item_id, new_count in progress_by_item.items():
            item = items_by_id.get(item_id)
            if not item:
                continue

            total_checks = max(1, len(item.acceptance_checks))
            decrement = new_count / total_checks

            if item.id in report.satisfied_hunger_item_ids:
                item.gap_score = 0.0
            else:
                item.gap_score = max(0.0, item.gap_score - decrement)
                if item.gap_score <= EPSILON:
                    item.gap_score = 0.0

            item.evidence_ids.extend(report.evidence_ids)
            item.updated_at_loop = report.loop_id

            if item.gap_score == 0.0:
                item.status = HungerItemStatus.VALIDATED_SATISFIED
            else:
                item.status = HungerItemStatus.WORKING

            mutated = True

        if mutated:
            self.repo.save_hunger_ledger(task_id, ledger)
