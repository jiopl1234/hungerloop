"""Minimal v0.5a ExecutionWorker (PRD §8.2).

The worker has one job: ask :class:`ModelClient` for a structured action
plan and dispatch each action through :class:`ToolHarness`. It never
touches :class:`CandidateState` directly — :class:`Integrator` synthesises
that from the returned :class:`WorkerResult` (PRD §8.1).

Day 5 ships the minimum needed to pump the demo loop end-to-end with
:class:`DummyModelClient.with_actions(...)` and the four MVP tools.
Day 9 swaps in :class:`OpenAIModelClient`; nothing in this module needs
to change because retry kwargs are already threaded through from
:attr:`ContextPack.budget` (PRD §28.2 / M6).
"""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.context import ContextPack
from hungerloop.models.worker import WorkerResult
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.model_client import ModelClient
from hungerloop.services.tool_harness import ToolHarness


class ExecutionWorker:
    """LLM-driven worker that emits tool actions and dispatches them.

    The :class:`Worker` Protocol is structurally satisfied — the runtime
    only needs ``async run(*, context, workspace_root)`` and that's what
    this class exposes.
    """

    def __init__(
        self,
        model_client: ModelClient,
        tool_harness: ToolHarness,
        repo: RepositoryProtocol,
        budget_guard: BudgetGuard | None = None,
    ) -> None:
        self.model_client = model_client
        self.tool_harness = tool_harness
        self.repo = repo
        self.budget_guard = budget_guard

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult:
        """Run one worker iteration.

        Flow (PRD §8.2):

        1. Build messages from the mission + acceptance criteria.
        2. Ask the model for ``{"summary": str, "actions": [...]}``;
           retry kwargs are sourced from ``context.budget`` so the retry
           loop in :class:`OpenAIModelClient` honours the per-loop policy.
        3. Dispatch every action through :class:`ToolHarness`.
        4. Aggregate evidence and artifact ids into a :class:`WorkerResult`.

        :class:`ToolHarness` may raise :class:`ToolNotPermitted` or the
        :class:`BudgetGuard` may raise :class:`WorkerBudgetExceeded`;
        both are caught by :class:`WorkerRuntime` rather than this method
        so we don't double-handle the same exceptions.
        """
        if self.budget_guard is not None:
            self.budget_guard.assert_can_spend(context, addl_llm_calls=1)

        response = await self.model_client.complete_json(
            task_id=context.task_id,
            loop_id=context.loop_id,
            agent_id=context.agent_id,
            messages=self._messages(context),
            max_tokens=context.budget.max_tokens,
            max_retries=context.budget.max_model_retries,
            retry_base_delay_seconds=context.budget.retry_base_delay_seconds,
            retry_max_delay_seconds=context.budget.retry_max_delay_seconds,
        )

        if self.budget_guard is not None:
            used_tokens = response.usage.input_tokens + response.usage.output_tokens
            self.budget_guard.record(
                context.task_id,
                context.loop_id,
                context.agent_id,
                tokens=used_tokens,
                llm_calls=1,
            )
            self.budget_guard.assert_can_spend(context)

        evidence_ids: list[str] = []
        artifact_ids: list[str] = []
        if response.evidence_id is not None:
            evidence_ids.append(response.evidence_id)

        actions = self._extract_actions(response.json_data)
        for action in actions:
            tool_name = self._stringify(action.get("tool_name", ""))
            args_raw = action.get("args", {})
            args = args_raw if isinstance(args_raw, dict) else {}

            result = await self.tool_harness.execute(
                context=context,
                tool_name=tool_name,
                args=args,
                workspace_root=workspace_root,
            )
            evidence_ids.extend(result.evidence_ids)
            artifact_ids.extend(result.artifact_ids)

        summary = self._stringify(
            (response.json_data or {}).get("summary", "")
        ) if response.json_data else ""

        return WorkerResult(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=summary,
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
        )

    @staticmethod
    def _messages(context: ContextPack) -> list[dict[str, str]]:
        """Build a minimal chat-completions message list (PRD §8.2 step 1)."""
        acceptance_lines = "\n".join(
            f"- {item}" for item in context.acceptance_criteria
        ) or "- (none provided)"
        system = (
            "You are an execution worker that emits structured JSON actions. "
            "Respond with a JSON object containing 'summary' (string) and "
            "'actions' (list of {tool_name, args})."
        )
        user = (
            f"Mission:\n{context.mission}\n\n"
            f"Acceptance criteria:\n{acceptance_lines}\n\n"
            f"Allowed tools: {', '.join(context.allowed_tools) or '(any)'}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _extract_actions(
        json_data: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        """Pull the ``actions`` list off the model response, defensively."""
        if not json_data:
            return []
        raw = json_data.get("actions")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _stringify(value: object) -> str:
        return value if isinstance(value, str) else str(value)
