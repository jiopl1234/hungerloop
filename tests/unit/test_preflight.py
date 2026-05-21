"""Unit tests for cli/preflight.py (PRD §13 / §18.3).

v0.5d.1 expands the matrix to all seven shipped StopReason values
(adding DONE → --reset and ERROR → repair-state gate) and pins the
``--skip-repair-check`` override path.
"""
from __future__ import annotations

import pytest

from hungerloop.cli.preflight import PreflightError, check_resume_preflight
from hungerloop.models.enums import HungerItemStatus, StopReason
from hungerloop.models.events import EventType
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
        # Anchor the gate on stop_report_created so ERROR comparisons have
        # something to compare against.
        repo.append_event(
            EventType.STOP_REPORT_CREATED,
            {"stop_reason": last_stop.value},
            task_id="t1",
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


def test_budget_exhausted_requires_refill_or_reset() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BUDGET_EXHAUSTED)
    with pytest.raises(PreflightError, match="BUDGET_EXHAUSTED"):
        check_resume_preflight(repo, "t1")


def test_budget_exhausted_passes_with_refill() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BUDGET_EXHAUSTED)
    check_resume_preflight(repo, "t1", refill_loops=5)


def test_budget_exhausted_passes_with_reset() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BUDGET_EXHAUSTED)
    check_resume_preflight(repo, "t1", reset=True)


def test_blocked_requires_unblock_all_or_open_items() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BLOCKED)
    with pytest.raises(PreflightError, match="BLOCKED"):
        check_resume_preflight(repo, "t1")


def test_blocked_passes_when_open_items_exist_with_resume() -> None:
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
    repo.append_event(
        EventType.STOP_REPORT_CREATED,
        {"stop_reason": "blocked"},
        task_id="t1",
    )
    check_resume_preflight(repo, "t1", resume_human=True)


def test_blocked_with_unblock_all_still_requires_resume() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BLOCKED)
    # --unblock-all alone is not enough — caller must also confirm the
    # resume so an accidental flag re-run doesn't loop straight back in.
    with pytest.raises(PreflightError, match="--resume"):
        check_resume_preflight(repo, "t1", unblock_all=True)


def test_blocked_passes_with_unblock_all_and_resume() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.BLOCKED)
    check_resume_preflight(repo, "t1", unblock_all=True, resume_human=True)


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


def test_safety_stop_with_ceiling_still_requires_resume() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.SAFETY_STOP)
    with pytest.raises(PreflightError, match="--resume"):
        check_resume_preflight(repo, "t1", raise_cost_ceiling=20.0)


def test_safety_stop_passes_with_higher_ceiling_and_resume() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.SAFETY_STOP)
    check_resume_preflight(
        repo, "t1", raise_cost_ceiling=20.0, resume_human=True
    )


def test_pending_mission_import_rejects_with_resume_notice() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_PAUSED)
    repo.save_evidence(
        task_id="t1",
        loop_id=None,
        evidence_type="human_input",
        payload={"kind": "mission_import", "success": True},
    )

    with pytest.raises(
        PreflightError,
        match="mission import pending; resume to apply on next commit",
    ):
        check_resume_preflight(repo, "t1")


def test_pending_mission_import_clears_after_state_regenerated() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_PAUSED)
    repo.save_evidence(
        task_id="t1",
        loop_id=None,
        evidence_type="human_input",
        payload={"kind": "mission_import", "success": True},
    )
    repo.append_event(
        EventType.MISSION_STATE_REGENERATED,
        {"mission_id": "mission-t1"},
        task_id="t1",
    )

    with pytest.raises(PreflightError, match="HUMAN_PAUSED") as exc_info:
        check_resume_preflight(repo, "t1")
    assert "mission import pending" not in str(exc_info.value)


def test_human_paused_requires_resume_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.HUMAN_PAUSED)
    with pytest.raises(PreflightError, match="HUMAN_PAUSED"):
        check_resume_preflight(repo, "t1")


# ---------------------------------------------------------------------------
# DONE — requires --reset (v0.5d.1)
# ---------------------------------------------------------------------------


def test_done_requires_reset_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.DONE)
    with pytest.raises(PreflightError, match="DONE"):
        check_resume_preflight(repo, "t1")


def test_done_passes_with_reset() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.DONE)
    check_resume_preflight(repo, "t1", reset=True)


# ---------------------------------------------------------------------------
# ERROR gate — requires --resume + repair_state_action (v0.5d.1)
# ---------------------------------------------------------------------------


def test_error_requires_resume_flag() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.ERROR)
    with pytest.raises(PreflightError, match="ERROR"):
        check_resume_preflight(repo, "t1")


def test_error_with_resume_but_no_repair_event_rejects() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.ERROR)
    with pytest.raises(PreflightError, match="repair-state"):
        check_resume_preflight(repo, "t1", resume_human=True)


def test_error_with_resume_and_recent_repair_event_passes() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.ERROR)
    repo.append_event(
        EventType.REPAIR_STATE_ACTION,
        {"action": "noop", "reason": "no divergences detected"},
        task_id="t1",
    )
    check_resume_preflight(repo, "t1", resume_human=True)


def test_error_with_skip_repair_check_passes() -> None:
    repo = InMemoryRepository()
    _seed(repo, last_stop=StopReason.ERROR)
    check_resume_preflight(
        repo,
        "t1",
        resume_human=True,
        skip_repair_check=True,
    )


@pytest.mark.parametrize(
    "stop_reason,kwargs",
    [
        (
            StopReason.DONE,
            {"reset": True},
        ),
        (
            StopReason.HUNGER_EXPIRED,
            {"refill_loops": 3},
        ),
        (
            StopReason.BUDGET_EXHAUSTED,
            {"refill_loops": 3},
        ),
        (
            StopReason.BLOCKED,
            {"unblock_all": True, "resume_human": True},
        ),
        (
            StopReason.HUMAN_REQUIRED,
            {"resume_human": True},
        ),
        (
            StopReason.HUMAN_PAUSED,
            {"resume_human": True},
        ),
        (
            StopReason.SAFETY_STOP,
            {"raise_cost_ceiling": 50.0, "resume_human": True},
        ),
        (
            StopReason.ERROR,
            {"resume_human": True, "skip_repair_check": True},
        ),
    ],
)
def test_full_matrix_with_required_flags_passes(
    stop_reason: StopReason, kwargs: dict[str, object]
) -> None:
    """FR-1 matrix, happy path: every shipped StopReason resolves
    cleanly when its required remediation flag is supplied."""
    repo = InMemoryRepository()
    _seed(repo, last_stop=stop_reason)
    check_resume_preflight(repo, "t1", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stop_reason",
    [
        StopReason.DONE,
        StopReason.HUNGER_EXPIRED,
        StopReason.BUDGET_EXHAUSTED,
        StopReason.BLOCKED,
        StopReason.HUMAN_REQUIRED,
        StopReason.HUMAN_PAUSED,
        StopReason.SAFETY_STOP,
        StopReason.ERROR,
    ],
)
def test_full_matrix_without_flags_rejects(stop_reason: StopReason) -> None:
    """FR-1 matrix, negative path: every shipped StopReason rejects when
    no remediation flag is supplied."""
    repo = InMemoryRepository()
    _seed(repo, last_stop=stop_reason)
    with pytest.raises(PreflightError):
        check_resume_preflight(repo, "t1")
