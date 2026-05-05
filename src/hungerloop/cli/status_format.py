"""Format ``hungerloop status <task_id>`` output (PRD §18.4).

Single pure function so the layout is unit-testable without invoking the
click command. Lines mirror the PRD §18.4 list verbatim except for two
additions: ``goal_status`` (the §28.6 / M20 label) is appended next to
``stop_reason`` because reading the bare reason on its own is rarely
useful — operators want the human-facing label too.
"""
from __future__ import annotations

from hungerloop.models.enums import HungerItemStatus
from hungerloop.repository.protocol import RepositoryProtocol


def format_status(repo: RepositoryProtocol, task_id: str) -> str:
    """Return a human-readable multiline status report for ``task_id``."""
    last_stop = repo.get_last_stop_reason(task_id)
    clock = repo.get_hunger_clock(task_id)
    usage = repo.get_usage_snapshot(task_id)
    best = repo.get_best_state(task_id)
    ledger = repo.get_hunger_ledger(task_id)

    open_items = [
        item.id
        for item in ledger.items
        if item.status in (HungerItemStatus.OPEN, HungerItemStatus.WORKING)
    ]
    blocked_items = [
        item.id for item in ledger.items if item.status == HungerItemStatus.BLOCKED
    ]
    paused_items = [
        item.id for item in ledger.items if item.status == HungerItemStatus.PAUSED
    ]

    accepted_count = len(best.accepted_check_keys) if best else 0
    best_id = best.state_id if best else None
    snapshot = repo.get_latest_hunger_snapshot(task_id)
    drive_budget = f"{snapshot.drive_budget:.2f}" if snapshot else "n/a (no snapshot yet)"

    lines = [
        f"task_id: {task_id}",
        f"stop_reason: {last_stop.value if last_stop else '(none)'}",
        f"loop_count: {clock.loop_count}",
        f"frozen: {clock.frozen}",
        f"current_drive_budget: {drive_budget}",
        f"phase: {snapshot.phase.value if snapshot else '(none)'}",
        f"active_hunger: {snapshot.active_hunger:.2f}" if snapshot else "active_hunger: n/a",
        f"work_pressure: {snapshot.work_pressure:.2f}" if snapshot else "work_pressure: n/a",
        f"total_cost_usd: {usage.cost_usd:.4f}",
        f"total_tokens: {usage.tokens}",
        f"llm_calls: {usage.llm_calls}",
        f"tool_calls: {usage.tool_calls}",
        f"best_state_id: {best_id or '(none)'}",
        f"accepted_check_keys_count: {accepted_count}",
        f"open hunger items: {open_items or '(none)'}",
        f"blocked hunger items: {blocked_items or '(none)'}",
        f"paused hunger items: {paused_items or '(none)'}",
    ]
    return "\n".join(lines)
