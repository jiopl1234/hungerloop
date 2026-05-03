"""WorkerRuntime — the thick shell around Worker dispatch (PRD §7).

The runtime sits between the Orchestrator and individual :class:`Worker`
implementations. It does not run worker logic itself; instead it:

1. Routes an :class:`AgentSpec` to the matching registered worker.
2. Pre-checks :class:`CostGuard` (task ceiling) and resets
   :class:`BudgetGuard` (per-loop counters; PRD §28.4 contract #1).
3. Wraps ``worker.run`` in :func:`asyncio.wait_for` so
   ``budget.max_wall_clock_seconds`` translates into a concrete timeout.
4. Maps every recoverable exception onto a :class:`WorkerResult` so the
   Orchestrator only needs one error path; non-recoverable errors
   (:class:`SafetyStopError`) are re-raised so the cost ceiling propagates.

§7.3 pseudocode is the contract; §28.4 / M12 supersedes the
``assert_worker_budget`` line — downstream services (ModelClient,
ToolHarness) self-check via :meth:`BudgetGuard.assert_can_spend`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from hungerloop.models.context import ContextPack
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_guard import BudgetGuard, WorkerBudgetExceeded
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.model_client import ModelAuthError, ModelCallError
from hungerloop.services.tool_harness import ToolNotPermitted

_UNKNOWN_PROVIDER = "unknown"
"""WorkerRuntime cannot know which provider raised — only the model client
does. Evidence rows reflect that with ``provider='unknown'``; OpenAIModelClient
writes its own evidence rows with the real provider/model when it owns the
retry loop (PRD §11.2)."""


class Worker(Protocol):
    """Minimal async surface every worker must implement (PRD §7.2)."""

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult: ...


class WorkerRuntime:
    """Dispatch a worker by ``spec.agent_id`` and map errors to :class:`WorkerResult`."""

    def __init__(
        self,
        workers: dict[str, Worker],
        cost_guard: CostGuard,
        budget_guard: BudgetGuard,
        repo: RepositoryProtocol,
    ) -> None:
        self.workers = workers
        self.cost_guard = cost_guard
        self.budget_guard = budget_guard
        self.repo = repo

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerResult:
        """Run the worker registered for ``spec.agent_id``.

        Returns a :class:`WorkerResult`. The result is the *only* output:

        - ``error is None`` indicates the worker completed normally.
        - ``error_type='worker_not_registered'`` — no worker for this id;
          configuration error (``requires_human=False``, ``retryable=False``).
        - ``error_type='worker_budget_exceeded'`` — :class:`BudgetGuard`
          rejected a downstream call.
        - ``error_type='auth_error'`` — model auth failed; ``requires_human=True``
          so the Orchestrator can stop with ``HUMAN_REQUIRED``.
        - ``error_type='model_call_error'`` — generic model failure;
          ``retryable`` mirrors the exception's flag.
        - ``error_type='timeout'`` — wall-clock cap hit; ``retryable=True``.

        :class:`SafetyStopError` is *re-raised* — it represents the global
        cost ceiling and must propagate to the Orchestrator (PRD §7.3).
        """
        worker = self.workers.get(spec.agent_id)
        if worker is None:
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error="worker_not_registered",
                error_type="configuration",
                requires_human=False,
                retryable=False,
            )

        try:
            self.cost_guard.assert_within_budget(context.task_id)
            self.budget_guard.reset(
                context.task_id, context.loop_id, context.agent_id
            )

            return await asyncio.wait_for(
                worker.run(context=context, workspace_root=workspace_root),
                timeout=context.budget.max_wall_clock_seconds,
            )

        except SafetyStopError:
            raise

        except WorkerBudgetExceeded as exc:
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error=str(exc),
                error_type="worker_budget_exceeded",
                requires_human=False,
                retryable=False,
            )

        except ToolNotPermitted as exc:
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error=str(exc),
                error_type="tool_not_permitted",
                requires_human=False,
                retryable=False,
            )

        except ModelAuthError as exc:
            evidence_id = self.repo.save_model_error_as_evidence(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=spec.agent_id,
                provider=_UNKNOWN_PROVIDER,
                model=_UNKNOWN_PROVIDER,
                error_type="auth_error",
                error_message=str(exc),
                retryable=False,
            )
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error=f"auth_error:{exc}",
                error_type="auth_error",
                requires_human=True,
                retryable=False,
                evidence_ids=[evidence_id],
            )

        except ModelCallError as exc:
            evidence_id = self.repo.save_model_error_as_evidence(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=spec.agent_id,
                provider=_UNKNOWN_PROVIDER,
                model=_UNKNOWN_PROVIDER,
                error_type="model_call_error",
                error_message=str(exc),
                retryable=exc.retryable,
            )
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error=f"model_call_error:{exc}",
                error_type="model_call_error",
                requires_human=False,
                retryable=exc.retryable,
                evidence_ids=[evidence_id],
            )

        except asyncio.TimeoutError:
            return WorkerResult(
                agent_id=spec.agent_id,
                task_id=context.task_id,
                loop_id=context.loop_id,
                error="worker_timeout",
                error_type="timeout",
                requires_human=False,
                retryable=True,
            )
