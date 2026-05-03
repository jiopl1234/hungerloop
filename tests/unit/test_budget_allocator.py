"""Unit tests for BudgetAllocator (PRD §4.1)."""
from __future__ import annotations

from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerSnapshot
from hungerloop.services.budget_allocator import BudgetAllocator


def _snapshot(phase: LoopPhase) -> HungerSnapshot:
    return HungerSnapshot(
        drive_budget=80.0,
        work_pressure=10.0,
        active_hunger=10.0,
        drive_ratio=0.8,
        phase=phase,
        should_stop=False,
    )


def test_explore_uses_explore_caps() -> None:
    alloc = BudgetAllocator()
    budget = alloc.allocate(_snapshot(LoopPhase.EXPLORE))
    assert budget.phase == LoopPhase.EXPLORE
    assert budget.max_tokens == 4000
    assert budget.max_tool_calls == 10
    assert budget.max_wall_clock_seconds == 300


def test_exploit_shortens_wall_clock() -> None:
    alloc = BudgetAllocator()
    budget = alloc.allocate(_snapshot(LoopPhase.EXPLOIT))
    assert budget.max_wall_clock_seconds == 180


def test_cooldown_caps_tokens_hard() -> None:
    alloc = BudgetAllocator()
    budget = alloc.allocate(_snapshot(LoopPhase.COOLDOWN))
    assert budget.max_tokens == 500
    assert budget.max_tool_calls == 1


def test_constructor_kwargs_override_defaults() -> None:
    alloc = BudgetAllocator(
        explore_max_tokens=99,
        allow_network=True,
        max_model_retries=5,
    )
    budget = alloc.allocate(_snapshot(LoopPhase.EXPLORE))
    assert budget.max_tokens == 99
    assert budget.allow_network is True
    assert budget.max_model_retries == 5


def test_phase_propagates_to_budget() -> None:
    alloc = BudgetAllocator()
    for phase in (LoopPhase.EXPLORE, LoopPhase.EXPLOIT, LoopPhase.COOLDOWN):
        budget = alloc.allocate(_snapshot(phase))
        assert budget.phase is phase
