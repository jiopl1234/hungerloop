"""Stateful per-(task, loop, agent) budget enforcement (PRD §28.4 / M12).

The v0.4.x ``BudgetGuard`` was stateless and trivially satisfiable; M12
re-grounded it as a per-call accumulator. The :class:`WorkerRuntime`
:meth:`reset`s the guard on entry; :class:`ModelClient` and
:class:`ToolHarness` then call :meth:`assert_can_spend` *before* each call
and :meth:`record` *after* each call so subsequent assertions reflect the
running total.

Three exhaustion modes raise :class:`WorkerBudgetExceeded`:

1. ``tokens`` past :attr:`BudgetAllocation.max_tokens`.
2. ``tool_calls`` past :attr:`BudgetAllocation.max_tool_calls`.
3. ``llm_calls`` past the (currently fixed) per-loop ceiling.

:class:`BudgetAllocation.max_wall_clock_seconds` is enforced separately by
``asyncio.wait_for`` in :class:`WorkerRuntime.run` — it does not flow
through this guard because ``elapsed_seconds`` would race against the
wall-clock interrupt anyway. The :attr:`BudgetUsage.elapsed_seconds`
field is recorded for observability only.
"""
from __future__ import annotations

from pydantic import BaseModel

from hungerloop.models.context import ContextPack

_LLM_CALL_CEILING_PER_LOOP: int = 999_999
"""Guard against runaway LLM call counts; PRD §28.4 leaves a single ceiling
for v0.5a until ``BudgetAllocation`` gets a dedicated ``max_llm_calls``."""


class WorkerBudgetExceeded(RuntimeError):
    """Raised when a worker would exceed its per-loop allocation."""


class BudgetUsage(BaseModel):
    """Mutable per-(task, loop, agent) running totals."""

    tokens: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    elapsed_seconds: float = 0.0


class BudgetGuard:
    """Stateful budget tracker keyed by ``(task_id, loop_id, agent_id)``."""

    def __init__(self) -> None:
        self._usage: dict[tuple[str, int, str], BudgetUsage] = {}

    def reset(self, task_id: str, loop_id: int, agent_id: str) -> None:
        """Drop any prior usage for the given key.

        Called by :class:`WorkerRuntime.run` on entry so a retry / second
        worker run starts from a clean slate (PRD §28.4 contract #1).
        """
        self._usage.pop((task_id, loop_id, agent_id), None)

    def record(
        self,
        task_id: str,
        loop_id: int,
        agent_id: str,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
        llm_calls: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        """Add the given deltas to the running total.

        Caller contract: invoke after every successful LLM/tool call so the
        next :meth:`assert_can_spend` sees the updated counters.
        """
        key = (task_id, loop_id, agent_id)
        cur = self._usage.get(key, BudgetUsage())
        cur.tokens += tokens
        cur.tool_calls += tool_calls
        cur.llm_calls += llm_calls
        cur.elapsed_seconds += elapsed_seconds
        self._usage[key] = cur

    def assert_can_spend(
        self,
        context: ContextPack,
        *,
        addl_tokens: int = 0,
        addl_tool_calls: int = 0,
        addl_llm_calls: int = 0,
    ) -> None:
        """Verify cumulative + ``addl_*`` stays within ``context.budget``.

        Raises:
            WorkerBudgetExceeded: when any axis would overflow. The error
                message includes the offending axis and current totals so
                the WorkerRuntime can surface it via
                ``WorkerResult.error_type='worker_budget_exceeded'``.
        """
        key = (context.task_id, context.loop_id, context.agent_id)
        cur = self._usage.get(key, BudgetUsage())
        budget = context.budget

        if cur.tokens + addl_tokens > budget.max_tokens:
            raise WorkerBudgetExceeded(
                f"token budget exceeded: {cur.tokens}+{addl_tokens} "
                f"> {budget.max_tokens}"
            )
        if cur.tool_calls + addl_tool_calls > budget.max_tool_calls:
            raise WorkerBudgetExceeded(
                f"tool_call budget exceeded: {cur.tool_calls}+{addl_tool_calls} "
                f"> {budget.max_tool_calls}"
            )
        if cur.llm_calls + addl_llm_calls > _LLM_CALL_CEILING_PER_LOOP:
            raise WorkerBudgetExceeded(
                f"llm_call budget exceeded: {cur.llm_calls}+{addl_llm_calls} "
                f"> {_LLM_CALL_CEILING_PER_LOOP}"
            )

    def usage_for(self, task_id: str, loop_id: int, agent_id: str) -> BudgetUsage:
        """Return a copy of current usage (or zero-valued default)."""
        return self._usage.get(
            (task_id, loop_id, agent_id), BudgetUsage()
        ).model_copy()
