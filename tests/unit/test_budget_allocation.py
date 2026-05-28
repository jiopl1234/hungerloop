"""Unit tests for BudgetAllocation v0.5a fields (PRD §4.2 / §28.11 / §28.18)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation


def test_defaults_match_prd() -> None:
    b = BudgetAllocation(phase=LoopPhase.EXPLORE)
    # NOTE: original PRD defaults (v0.4.1) were max_tokens=4000,
    # max_tool_calls=10 — sized for strictly single-call workers. With the
    # A-patched ExecutionWorker (inner self-repair, up to
    # MAX_SELF_REPAIR_ITERATIONS+1 model calls per loop) those caps trigger
    # worker_budget_exceeded after one reasoning-heavy call. Bumped to
    # 60K / 80 to give the inner loop real headroom.
    assert b.max_tokens == 60000
    assert b.max_tool_calls == 80
    assert b.max_wall_clock_seconds == 300
    assert b.max_workers_per_loop == 1
    assert b.max_model_retries == 4
    assert b.retry_base_delay_seconds == 1.0
    assert b.retry_max_delay_seconds == 20.0
    assert b.allow_shell is True
    assert b.allow_file_write is True
    assert b.allow_network is False


def test_phase_required() -> None:
    with pytest.raises(ValidationError):
        BudgetAllocation()  # type: ignore[call-arg]


def test_negative_max_tokens_rejected() -> None:
    with pytest.raises(ValidationError):
        BudgetAllocation(phase=LoopPhase.EXPLORE, max_tokens=-1)


def test_zero_wall_clock_rejected() -> None:
    """max_wall_clock_seconds must be >= 1 so asyncio.wait_for is meaningful."""
    with pytest.raises(ValidationError):
        BudgetAllocation(phase=LoopPhase.EXPLORE, max_wall_clock_seconds=0)


def test_zero_max_workers_per_loop_rejected() -> None:
    with pytest.raises(ValidationError):
        BudgetAllocation(phase=LoopPhase.EXPLORE, max_workers_per_loop=0)


def test_retry_max_below_base_rejected() -> None:
    """model_validator must reject retry_max < retry_base."""
    with pytest.raises(ValidationError, match="retry_max_delay_seconds"):
        BudgetAllocation(
            phase=LoopPhase.EXPLORE,
            retry_base_delay_seconds=5.0,
            retry_max_delay_seconds=1.0,
        )


def test_retry_max_equal_base_accepted() -> None:
    b = BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        retry_base_delay_seconds=3.0,
        retry_max_delay_seconds=3.0,
    )
    assert b.retry_max_delay_seconds == 3.0


def test_network_default_denied() -> None:
    """allow_network defaults False — network access is opt-in per ADR-003."""
    b = BudgetAllocation(phase=LoopPhase.COOLDOWN)
    assert b.allow_network is False
