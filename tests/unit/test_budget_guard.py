"""Unit tests for stateful BudgetGuard (PRD §28.4 / M12)."""
from __future__ import annotations

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.budget_guard import (
    BudgetGuard,
    BudgetUsage,
    WorkerBudgetExceeded,
)


def _ctx(*, max_tokens: int = 100, max_tool_calls: int = 5) -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="m",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="cand",
        budget=BudgetAllocation(
            phase=LoopPhase.EXPLORE,
            max_tokens=max_tokens,
            max_tool_calls=max_tool_calls,
        ),
    )


def test_assert_can_spend_passes_with_zero_usage() -> None:
    guard = BudgetGuard()
    guard.assert_can_spend(_ctx(), addl_tokens=10, addl_tool_calls=1)


def test_record_then_assert_uses_running_total() -> None:
    guard = BudgetGuard()
    ctx = _ctx(max_tokens=100)
    guard.record(ctx.task_id, ctx.loop_id, ctx.agent_id, tokens=80)
    guard.assert_can_spend(ctx, addl_tokens=20)
    with pytest.raises(WorkerBudgetExceeded, match="token budget"):
        guard.assert_can_spend(ctx, addl_tokens=21)


def test_tool_call_overflow_raises() -> None:
    guard = BudgetGuard()
    ctx = _ctx(max_tool_calls=2)
    guard.record(ctx.task_id, ctx.loop_id, ctx.agent_id, tool_calls=2)
    with pytest.raises(WorkerBudgetExceeded, match="tool_call budget"):
        guard.assert_can_spend(ctx, addl_tool_calls=1)


def test_reset_clears_usage() -> None:
    guard = BudgetGuard()
    ctx = _ctx(max_tokens=100)
    guard.record(ctx.task_id, ctx.loop_id, ctx.agent_id, tokens=99)
    guard.reset(ctx.task_id, ctx.loop_id, ctx.agent_id)
    guard.assert_can_spend(ctx, addl_tokens=100)


def test_reset_is_per_key() -> None:
    guard = BudgetGuard()
    guard.record("t1", 1, "a", tokens=50)
    guard.record("t1", 2, "a", tokens=50)
    guard.reset("t1", 1, "a")
    assert guard.usage_for("t1", 1, "a").tokens == 0
    assert guard.usage_for("t1", 2, "a").tokens == 50


def test_usage_for_returns_copy() -> None:
    guard = BudgetGuard()
    guard.record("t1", 1, "a", tokens=10, tool_calls=1, llm_calls=1, elapsed_seconds=0.5)
    snap1 = guard.usage_for("t1", 1, "a")
    snap1.tokens = 999
    snap2 = guard.usage_for("t1", 1, "a")
    assert snap2.tokens == 10


def test_usage_for_unknown_key_returns_zero() -> None:
    guard = BudgetGuard()
    snap = guard.usage_for("t1", 1, "a")
    assert snap == BudgetUsage()


def test_record_accumulates_all_axes() -> None:
    guard = BudgetGuard()
    guard.record(
        "t1", 1, "a",
        tokens=10, tool_calls=1, llm_calls=1, elapsed_seconds=0.25,
    )
    guard.record(
        "t1", 1, "a",
        tokens=5, tool_calls=2, llm_calls=1, elapsed_seconds=0.75,
    )
    snap = guard.usage_for("t1", 1, "a")
    assert snap.tokens == 15
    assert snap.tool_calls == 3
    assert snap.llm_calls == 2
    assert snap.elapsed_seconds == 1.0
