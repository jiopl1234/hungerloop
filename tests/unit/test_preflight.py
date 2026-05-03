"""Unit tests for cli/preflight.py (PRD §18.3)."""
from __future__ import annotations

import pytest

from hungerloop.cli.preflight import PreflightError, check_resume_preflight
from hungerloop.models.enums import HungerItemStatus, StopReason
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.tracing import StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository


def _seed(repo: InMemoryRepository, *, last_stop: StopReason | None = None) -> None:
    repo.set_hunger_policy("t1", HungerPolicy(max_total_cost_usd=10.0))
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    if last_stop is not None:
        repo.save_stop_report(
            StopReport(task_id="t1", stop_reason=last_stop, goal_status="abandoned")
        )


def test_no_prior_stop_passes() -> None:
    repo = InMemoryRepository()
    _seed(repo)
    check_resume_preflight(repo, "t1")  # no raise


def test_hunger_expired_requires_refill() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUNGER_EXPIRED)
    with pytest.raises(PreflightError, match="HUNGER_EXPIRED"):
        check_resume_preflight(repo, "t1")


def test_hunger_expired_passes_with_refill() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUNGER_EXPIRED)
    check_resume_preflight(repo, "t1", refill_loops=5)


def test_blocked_requires_unblock_all_or_open_items() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BLOCKED)
    with pytest.raises(PreflightError, match="BLOCKED"):
        check_resume_preflight(repo, "t1")


def test_blocked_passes_when_open_items_exist() -> None:
    repo = InMemoryRepository()
    repo.set_hunger_policy("t1", HungerPolicy(max_total_cost_usd=10.0))
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-001",
                    title="x",
                    status=HungerItemStatus.OPEN,
                    gap_score=1.0,
                )
            ],
        ),
    )
    repo.save_stop_report(
        StopReport(
            task_id="t1", stop_reason=StopReason.BLOCKED, goal_status="blocked"
        )
    )
    check_resume_preflight(repo, "t1")  # has active items


def test_blocked_passes_with_unblock_all_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BLOCKED)
    check_resume_preflight(repo, "t1", unblock_all=True)


def test_human_required_requires_resume_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_REQUIRED)
    with pytest.raises(PreflightError, match="HUMAN_REQUIRED"):
        check_resume_preflight(repo, "t1")


def test_human_required_passes_with_resume_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_REQUIRED)
    check_resume_preflight(repo, "t1", resume_human=True)


def test_safety_stop_requires_raise_cost_ceiling() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.SAFETY_STOP)
    with pytest.raises(PreflightError, match="SAFETY_STOP"):
        check_resume_preflight(repo, "t1")


def test_safety_stop_rejects_lower_ceiling() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.SAFETY_STOP)
    with pytest.raises(PreflightError, match="must exceed"):
        check_resume_preflight(repo, "t1", raise_cost_ceiling=5.0)


def test_safety_stop_passes_with_higher_ceiling() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.SAFETY_STOP)
    check_resume_preflight(repo, "t1", raise_cost_ceiling=20.0)


def test_human_paused_requires_resume_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_PAUSED)
    with pytest.raises(PreflightError, match="HUMAN_PAUSED"):
        check_resume_preflight(repo, "t1")


def test_done_passes_without_action() -> None:
    """Re-running a DONE task is allowed; it'll emit DONE again."""
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.DONE)
    check_resume_preflight(repo, "t1")
