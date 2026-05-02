"""Cost ceiling enforcement for HungerLoop v0.4.1.

:class:`CostGuard` enforces invariant I-8: the agent stops when cumulative cost
or token usage exceeds the policy ceiling. The guard is called before and after
every LLM/tool invocation to prevent runaway spend.

:class:`SafetyStopError` is raised when a ceiling is hit.
"""
from __future__ import annotations

from hungerloop.models.usage import ModelUsage
from hungerloop.repository.protocol import RepositoryProtocol


class SafetyStopError(RuntimeError):
    """Raised when cost or token ceiling is exceeded."""
    pass


class CostGuard:
    """Enforce cost and token ceilings."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def assert_within_budget(self, task_id: str) -> None:
        """Check if current usage is within policy limits.

        Args:
            task_id: Task identifier.

        Raises:
            SafetyStopError: If cost or token ceiling is exceeded.
        """
        policy = self.repo.get_hunger_policy(task_id)
        clock = self.repo.get_hunger_clock(task_id)

        if clock.consumed_by_cost_usd >= policy.max_total_cost_usd:
            raise SafetyStopError(
                f"Cost ceiling hit: ${clock.consumed_by_cost_usd:.4f}"
                f" >= ${policy.max_total_cost_usd:.4f}"
            )

        if clock.consumed_tokens >= policy.max_total_tokens:
            raise SafetyStopError(
                f"Token ceiling hit: {clock.consumed_tokens}"
                f" >= {policy.max_total_tokens}"
            )

    def record_llm_usage(self, task_id: str, usage: ModelUsage) -> None:
        """Record LLM usage and check budget.

        Args:
            task_id: Task identifier.
            usage: Usage object with ``input_tokens``, ``output_tokens``, ``cost_usd``.

        Raises:
            SafetyStopError: If the update pushes usage over the ceiling.
        """
        clock = self.repo.get_hunger_clock(task_id)
        clock.consumed_tokens += usage.input_tokens + usage.output_tokens
        clock.consumed_by_cost_usd += usage.cost_usd
        self.repo.save_hunger_clock(clock)
        self.assert_within_budget(task_id)

    def record_tool_cost(
        self, task_id: str, cost_usd: float = 0.0, tokens: int = 0
    ) -> None:
        """Record tool cost and check budget.

        Args:
            task_id: Task identifier.
            cost_usd: Cost in USD.
            tokens: Token count (for tools that consume tokens, e.g., embeddings).

        Raises:
            SafetyStopError: If the update pushes usage over the ceiling.
        """
        clock = self.repo.get_hunger_clock(task_id)
        clock.consumed_tokens += tokens
        clock.consumed_by_cost_usd += cost_usd
        self.repo.save_hunger_clock(clock)
        self.assert_within_budget(task_id)
