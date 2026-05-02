"""Context builder service for HungerLoop v0.4.1.

:class:`ContextBuilder` constructs agent execution contexts from repository state.
"""
from __future__ import annotations

from typing import Any

from hungerloop.models.context import ContextPack
from hungerloop.models.planning import BudgetAllocation


class ContextBuilder:
    """Build agent execution contexts."""

    def __init__(self, repo: Any) -> None:
        # TODO(Task 14): tighten ``repo`` to the Repository protocol once it lands.
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
                acceptance_criteria.append(check.description)

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
