"""Unit tests for WorkerRuntime (PRD §7)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.budget_guard import BudgetGuard, WorkerBudgetExceeded
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.model_client import ModelAuthError, ModelCallError
from hungerloop.services.worker_runtime import WorkerRuntime

_AGENT_ID = "execution_worker_v1"


def _spec() -> AgentSpec:
    return AgentSpec(
        agent_id=_AGENT_ID,
        name="ExecutionWorkerV1",
        kind="execution",
        allowed_tools=["read_file"],
    )


def _context(*, max_wall_clock_seconds: int = 30) -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id=_AGENT_ID,
        mission="do thing",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="cand",
        budget=BudgetAllocation(
            phase=LoopPhase.EXPLORE,
            max_wall_clock_seconds=max_wall_clock_seconds,
        ),
    )


@pytest.fixture
def repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "t1", HungerPolicy(max_total_cost_usd=10.0, max_total_tokens=1_000_000)
    )
    # Lazily auto-creates a fresh HungerClockState; CostGuard reads via
    # get_hunger_clock so we don't need an explicit save here.
    repo.get_hunger_clock("t1")
    return repo


@pytest.fixture
def cost_guard(repo: InMemoryRepository) -> CostGuard:
    return CostGuard(repo)


class _FakeWorker:
    """Configurable worker for runtime tests."""

    def __init__(
        self,
        *,
        result: WorkerResult | None = None,
        raises: BaseException | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self.result = result
        self.raises = raises
        self.sleep_seconds = sleep_seconds
        self.invocations = 0

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult:
        self.invocations += 1
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.raises is not None:
            raise self.raises
        if self.result is not None:
            return self.result
        return WorkerResult(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary="ok",
        )


async def test_happy_path_returns_worker_result(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    worker = _FakeWorker()
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    result = await runtime.run(_spec(), _context(), Path("/tmp"))

    assert result.error is None
    assert result.summary == "ok"
    assert worker.invocations == 1


async def test_missing_worker_returns_configuration_error(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    runtime = WorkerRuntime({}, cost_guard, BudgetGuard(), repo)
    result = await runtime.run(_spec(), _context(), Path("/tmp"))

    assert result.error == "worker_not_registered"
    assert result.error_type == "configuration"
    assert result.requires_human is False
    assert result.retryable is False


async def test_budget_guard_reset_on_entry(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    """Per PRD §28.4 contract #1: WorkerRuntime resets budget guard on entry."""
    guard = BudgetGuard()
    guard.record("t1", 1, _AGENT_ID, tokens=999, tool_calls=99)
    runtime = WorkerRuntime({_AGENT_ID: _FakeWorker()}, cost_guard, guard, repo)

    await runtime.run(_spec(), _context(), Path("/tmp"))
    assert guard.usage_for("t1", 1, _AGENT_ID).tokens == 0
    assert guard.usage_for("t1", 1, _AGENT_ID).tool_calls == 0


async def test_worker_budget_exceeded_maps_to_result(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    worker = _FakeWorker(raises=WorkerBudgetExceeded("tool_call budget exceeded"))
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    result = await runtime.run(_spec(), _context(), Path("/tmp"))

    assert result.error_type == "worker_budget_exceeded"
    assert result.retryable is False
    assert "tool_call" in (result.error or "")


async def test_model_auth_error_requires_human(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    worker = _FakeWorker(raises=ModelAuthError("openai 401"))
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    result = await runtime.run(_spec(), _context(), Path("/tmp"))

    assert result.error_type == "auth_error"
    assert result.requires_human is True
    assert result.retryable is False
    assert len(result.evidence_ids) == 1
    evidence_id = result.evidence_ids[0]
    assert repo._evidence[evidence_id]["error_type"] == "auth_error"
    assert repo._evidence[evidence_id]["retryable"] is False


async def test_model_call_error_propagates_retryable_flag(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    worker = _FakeWorker(raises=ModelCallError("server 500", retryable=True))
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    result = await runtime.run(_spec(), _context(), Path("/tmp"))

    assert result.error_type == "model_call_error"
    assert result.requires_human is False
    assert result.retryable is True
    assert len(result.evidence_ids) == 1
    assert repo._evidence[result.evidence_ids[0]]["retryable"] is True


async def test_timeout_returns_retryable_result(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    worker = _FakeWorker(sleep_seconds=0.5)
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    # max_wall_clock_seconds must be >=1 by validation; use 1 and a 2s sleeper.
    worker.sleep_seconds = 2.0
    ctx = _context(max_wall_clock_seconds=1)
    result = await runtime.run(_spec(), ctx, Path("/tmp"))

    assert result.error_type == "timeout"
    assert result.retryable is True
    assert result.error == "worker_timeout"


async def test_safety_stop_re_raises(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    """SafetyStopError must propagate so the Orchestrator can stop the task."""
    worker = _FakeWorker(raises=SafetyStopError("token ceiling"))
    runtime = WorkerRuntime({_AGENT_ID: worker}, cost_guard, BudgetGuard(), repo)
    with pytest.raises(SafetyStopError):
        await runtime.run(_spec(), _context(), Path("/tmp"))


async def test_safety_stop_from_cost_guard_re_raises(
    repo: InMemoryRepository, cost_guard: CostGuard
) -> None:
    """Pre-call cost_guard.assert_within_budget can also raise SafetyStopError."""
    repo.set_hunger_policy(
        "t1", HungerPolicy(max_total_cost_usd=0.0, max_total_tokens=1_000_000)
    )
    runtime = WorkerRuntime({_AGENT_ID: _FakeWorker()}, cost_guard, BudgetGuard(), repo)
    with pytest.raises(SafetyStopError, match="Cost ceiling"):
        await runtime.run(_spec(), _context(), Path("/tmp"))
