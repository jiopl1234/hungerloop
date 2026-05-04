"""Unit tests for HungerEngine — direct coverage of decay paths.

`hunger_engine.py` had ~67% coverage before this file landed; LOOP_COUNT
was exercised through test_loop_count_decay but STAGE_BASED was only
covered by enum tests, and the LOOP_COUNT max_loops_for_decay=0 boundary
was untested.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hungerloop.models.enums import DecayType
from hungerloop.models.hunger import HungerClockState, HungerPolicy
from hungerloop.services.hunger_engine import HungerEngine


def test_stage_based_decay_at_50_percent_elapsed() -> None:
    engine = HungerEngine()
    now = datetime.now(timezone.utc)
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=100.0,
        started_at=now - timedelta(seconds=50),
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(policy, clock, now)
    assert 40.0 < budget < 60.0, (
        f"STAGE_BASED at 50% elapsed should yield ~50; got {budget}"
    )


def test_stage_based_decay_past_duration_returns_zero() -> None:
    engine = HungerEngine()
    now = datetime.now(timezone.utc)
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=10.0,
        started_at=now - timedelta(seconds=999),
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(policy, clock, now)
    assert budget == 0.0


def test_stage_based_decay_with_no_started_at_returns_initial() -> None:
    """STAGE_BASED needs `started_at`; without it, fall through to
    `initial_hunger` instead of crashing."""
    engine = HungerEngine()
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.STAGE_BASED,
        decay_duration_seconds=100.0,
        started_at=None,
    )
    clock = HungerClockState(task_id="t1")
    budget = engine._compute_drive_budget(
        policy, clock, datetime.now(timezone.utc)
    )
    assert budget == 100.0


def test_loop_count_max_zero_does_not_divide_by_zero() -> None:
    """Boundary: LOOP_COUNT decay with max_loops_for_decay=0 must not
    raise ZeroDivisionError."""
    engine = HungerEngine()
    policy = HungerPolicy(
        initial_hunger=100.0,
        decay_type=DecayType.LOOP_COUNT,
        max_loops_for_decay=0,
    )
    clock = HungerClockState(task_id="t1")
    try:
        budget = engine._compute_drive_budget(
            policy, clock, datetime.now(timezone.utc)
        )
    except ZeroDivisionError:
        pytest.fail(
            "LOOP_COUNT with max_loops_for_decay=0 raised ZeroDivisionError "
            "instead of returning a sensible value"
        )
    assert isinstance(budget, float)
