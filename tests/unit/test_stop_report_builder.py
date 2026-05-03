"""Unit tests for build_stop_report mapping (PRD §28.6 / M20)."""
from __future__ import annotations

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import StopReason
from hungerloop.models.hunger import HungerItem, HungerLedger
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.stop_report_builder import build_stop_report


def _seed_ledger(repo: InMemoryRepository, items: list[HungerItem]) -> None:
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))


@pytest.mark.parametrize(
    "stop_reason,expected_status",
    [
        (StopReason.DONE, "completed"),
        (StopReason.BLOCKED, "blocked"),
        (StopReason.HUMAN_REQUIRED, "paused"),
        (StopReason.HUMAN_PAUSED, "paused"),
        (StopReason.SAFETY_STOP, "abandoned"),
        (StopReason.ERROR, "abandoned"),
    ],
)
def test_simple_mapping(stop_reason: StopReason, expected_status: str) -> None:
    repo = InMemoryRepository()
    _seed_ledger(repo, [])
    report = build_stop_report(repo, "t1", stop_reason)
    assert report.goal_status == expected_status
    assert report.stop_reason is stop_reason


def test_hunger_expired_partial_when_best_has_accepted_checks() -> None:
    repo = InMemoryRepository()
    _seed_ledger(repo, [])
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="BS-1",
            summary="partial work",
            accepted_check_keys=["H-001:0"],
        )
    )
    report = build_stop_report(repo, "t1", StopReason.HUNGER_EXPIRED)
    assert report.goal_status == "partial"
    assert report.accepted_check_keys_count == 1
    assert report.final_best_state_id == "BS-1"


def test_hunger_expired_abandoned_when_no_useful_best() -> None:
    repo = InMemoryRepository()
    _seed_ledger(repo, [])
    report = build_stop_report(repo, "t1", StopReason.HUNGER_EXPIRED)
    assert report.goal_status == "abandoned"


def test_hunger_expired_abandoned_when_best_has_no_accepted_checks() -> None:
    """Best exists but accepted_check_keys is empty -> still 'abandoned'."""
    repo = InMemoryRepository()
    _seed_ledger(repo, [])
    repo.save_best_state(
        BestState(task_id="t1", state_id="BS-1", summary="no progress")
    )
    report = build_stop_report(repo, "t1", StopReason.HUNGER_EXPIRED)
    assert report.goal_status == "abandoned"


def test_remaining_and_blocked_items_populated() -> None:
    from hungerloop.models.enums import HungerItemStatus

    repo = InMemoryRepository()
    items = [
        HungerItem(id="H-001", title="open", status=HungerItemStatus.OPEN),
        HungerItem(id="H-002", title="blocked", status=HungerItemStatus.BLOCKED),
        HungerItem(id="H-003", title="done", status=HungerItemStatus.CLOSED),
    ]
    _seed_ledger(repo, items)
    report = build_stop_report(repo, "t1", StopReason.BLOCKED)
    assert report.remaining_hunger_items == ["H-001", "H-002"]
    assert report.blocked_hunger_items == ["H-002"]


def test_totals_from_repo() -> None:
    repo = InMemoryRepository()
    _seed_ledger(repo, [])
    repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a",
        provider="dummy",
        model="dummy",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.5,
        response_preview="",
    )
    clock = repo.get_hunger_clock("t1")
    clock.loop_count = 4
    repo.save_hunger_clock(clock)
    report = build_stop_report(repo, "t1", StopReason.DONE)
    assert report.total_loops == 4
    assert report.total_tokens == 30
    assert report.total_cost_usd == 0.5
