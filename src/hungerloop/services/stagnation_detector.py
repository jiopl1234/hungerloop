"""StagnationDetector service for HungerLoop v0.4.1.

Tracks per-item consecutive failures and global no-progress streaks. When an item
reaches the failure threshold, it is marked BLOCKED. When the global no-progress
streak exceeds the threshold, the orchestrator should emit StopReason.BLOCKED.

Only items in ``attempted_hunger_item_ids`` are counted; unattempted items are
ignored (invariant I-8: attempted-only tracking).
"""
from __future__ import annotations

from typing import Any

from hungerloop.models.enums import HungerItemStatus


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
        repo: Any,
        max_item_consecutive_failures: int = 3,
        max_global_no_progress_loops: int = 5,
    ) -> None:
        self.repo = repo
        self.max_item_failures = max_item_consecutive_failures
        self.max_global_no_progress = max_global_no_progress_loops

    def update(
        self, task_id: str, loop_id: int, validation_report: Any
    ) -> dict[str, object]:
        """Update stagnation counters based on validation report.

        Args:
            task_id: Task identifier.
            loop_id: Current loop number.
            validation_report: ValidationReport with attempted_hunger_item_ids,
                newly_passed_check_keys, and has_real_progress.

        Returns:
            Dictionary with:
                - blocked_items: List of item IDs newly marked BLOCKED.
                - global_blocked: True if global no-progress streak exceeded threshold.
        """
        attempted = set(validation_report.attempted_hunger_item_ids)
        newly_progressed: set[str] = set()

        for check_key in validation_report.newly_passed_check_keys:
            item_id = check_key.split(":", 1)[0]
            newly_progressed.add(item_id)

        blocked_items: list[str] = []

        for iid in attempted:
            item = self.repo.get_hunger_item(iid)
            if item is None:
                continue

            if iid in newly_progressed:
                item.consecutive_failure_count = 0
                item.last_progress_loop_id = loop_id
            else:
                item.consecutive_failure_count += 1

            if item.consecutive_failure_count >= self.max_item_failures:
                item.status = HungerItemStatus.BLOCKED
                blocked_items.append(iid)

            self.repo.save_hunger_item(item)

        if validation_report.has_real_progress:
            self.repo.reset_no_progress_streak(task_id)
            global_blocked = False
        else:
            streak: int = self.repo.increment_no_progress_streak(task_id)
            global_blocked = streak >= self.max_global_no_progress

        return {
            "blocked_items": blocked_items,
            "global_blocked": global_blocked,
        }
