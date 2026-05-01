"""HungerUpdateService for HungerLoop v0.4.1.

Applies validation outcomes to hunger items by decrementing their ``gap_score``
proportionally to the number of newly-passed acceptance checks (check-level
progress, invariant I-3). When an item reaches zero gap AND is listed in
``satisfied_hunger_item_ids``, its status transitions to VALIDATED_SATISFIED.
Otherwise an item with progress becomes WORKING.

FAIL verdicts are a no-op — no writes occur.
"""
from __future__ import annotations

from typing import Any

from hungerloop.models.enums import HungerItemStatus, ValidationVerdict


class HungerUpdateService:
    """Applies validation outcomes to hunger items."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def apply_validation(self, task_id: str, report: Any) -> None:
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
            item_id, _ = key.split(":", 1)
            progress_by_item[item_id] = progress_by_item.get(item_id, 0) + 1

        for item_id, new_count in progress_by_item.items():
            item = self.repo.get_hunger_item(item_id)
            if not item:
                continue

            total_checks = max(1, len(item.acceptance_checks))
            decrement = new_count / total_checks

            item.gap_score = max(0.0, item.gap_score - decrement)
            item.evidence_ids.extend(report.evidence_ids)
            item.updated_at_loop = report.loop_id

            if (
                item.id in report.satisfied_hunger_item_ids
                and item.gap_score == 0.0
            ):
                item.status = HungerItemStatus.VALIDATED_SATISFIED
            else:
                item.status = HungerItemStatus.WORKING

            self.repo.save_hunger_item(item)
