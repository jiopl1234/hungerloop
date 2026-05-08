"""Context builder service for HungerLoop v0.4.1.

:class:`ContextBuilder` constructs agent execution contexts from repository state.
"""
from __future__ import annotations

import json

from hungerloop.models.context import ContextPack
from hungerloop.models.hunger import AcceptanceCheck
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.protocol import RepositoryProtocol


def _format_check(check: AcceptanceCheck) -> str:
    """Render one acceptance check as a single line carrying both the
    human description and the machine-checkable params.

    The params field contains the actual semantics the validator will
    enforce (file paths, argv assertions, etc.). Without them the worker
    only sees a description like "pytest passes" and has to guess what
    the test actually checks. With them the worker can read e.g.
    ``argv=['python','-c','assert fizzbuzz(3)=="Fizz"']`` and infer the
    rule directly.
    """
    desc = (check.description or check.check_type.value).strip()
    try:
        params_blob = json.dumps(check.params, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        params_blob = str(check.params)
    return f"{desc} [{check.check_type.value} params={params_blob}]"


class ContextBuilder:
    """Build agent execution contexts."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def build_for_agent(
        self,
        task_id: str,
        loop_id: int,
        agent_id: str,
        mission: str,
        target_hunger_item_ids: list[str],
        budget: BudgetAllocation,
        allowed_tools: list[str],
        output_schema_name: str,
        candidate_workspace_ref: str,
    ) -> ContextPack:
        """Build a context pack for an agent.

        Args:
            task_id: Task identifier.
            loop_id: Loop iteration.
            agent_id: Agent identifier.
            mission: Agent's mission description.
            target_hunger_item_ids: Hunger items to work on.
            budget: Budget allocation for this phase.
            allowed_tools: Tools the agent can use.
            output_schema_name: Required output schema.
            candidate_workspace_ref: Workspace reference for the candidate.

        Returns:
            A context pack for the agent.
        """
        best = self.repo.get_best_state(task_id)

        items = self.repo.get_hunger_items(target_hunger_item_ids)
        acceptance_criteria = []
        for item in items:
            for check in item.acceptance_checks:
                acceptance_criteria.append(_format_check(check))

        return ContextPack(
            task_id=task_id,
            loop_id=loop_id,
            agent_id=agent_id,
            mission=mission,
            phase=budget.phase.value,
            target_hunger_item_ids=target_hunger_item_ids,
            acceptance_criteria=acceptance_criteria,
            best_state_summary=best.summary if best else None,
            candidate_workspace_ref=candidate_workspace_ref,
            allowed_tools=allowed_tools,
            budget=budget,
            required_output_schema=output_schema_name,
        )
