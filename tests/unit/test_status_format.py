"""Unit tests for cli/status_format.py (PRD §18.4)."""
from __future__ import annotations

from hungerloop.cli.status_format import format_status
from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import HungerItemStatus, LoopPhase, StopReason
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerSnapshot
from hungerloop.models.tracing import StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository


def test_fresh_task_shows_no_stop_reason() -> None:
    repo = InMemoryRepository()
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    out = format_status(repo, "t1")
    assert "task_id: t1" in out
    assert "stop_reason: (none)" in out
    assert "best_state_id: (none)" in out
    assert "loop_count: 0" in out


def test_status_includes_open_blocked_paused() -> None:
    repo = InMemoryRepository()
    items = [
        HungerItem(id="H-001", title="open", status=HungerItemStatus.OPEN),
        HungerItem(id="H-002", title="blocked", status=HungerItemStatus.BLOCKED),
        HungerItem(id="H-003", title="paused", status=HungerItemStatus.PAUSED),
        HungerItem(id="H-004", title="working", status=HungerItemStatus.WORKING),
    ]
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))
    for item in items:
        repo.save_hunger_item(item)
    out = format_status(repo, "t1")
    assert "open hunger items: ['H-001', 'H-004']" in out
    assert "blocked hunger items: ['H-002']" in out
    assert "paused hunger items: ['H-003']" in out


def test_status_reports_best_state() -> None:
    repo = InMemoryRepository()
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="BS-1",
            summary="ok",
            accepted_check_keys=["H-001:0", "H-002:0"],
        )
    )
    out = format_status(repo, "t1")
    assert "best_state_id: BS-1" in out
    assert "accepted_check_keys_count: 2" in out


def test_status_uses_last_stop_reason() -> None:
    repo = InMemoryRepository()
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUNGER_EXPIRED,
            goal_status="abandoned",
        )
    )
    out = format_status(repo, "t1")
    assert "stop_reason: hunger_expired" in out


def test_status_shows_clock_frozen_state() -> None:
    repo = InMemoryRepository()
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    clock = repo.get_hunger_clock("t1")
    clock.frozen = True
    repo.save_hunger_clock(clock)
    out = format_status(repo, "t1")
    assert "frozen: True" in out


def test_status_shows_latest_hunger_snapshot() -> None:
    repo = InMemoryRepository()
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    repo.save_hunger_snapshot(
        "t1",
        HungerSnapshot(
            drive_budget=42.5,
            work_pressure=3.0,
            active_hunger=3.0,
            drive_ratio=0.4,
            phase=LoopPhase.EXPLOIT,
            should_stop=False,
        ),
    )
    out = format_status(repo, "t1")
    assert "current_drive_budget: 42.50" in out
    assert "phase: exploit" in out
    assert "active_hunger: 3.00" in out
    assert "work_pressure: 3.00" in out
