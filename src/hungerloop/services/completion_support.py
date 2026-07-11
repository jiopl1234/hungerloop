"""Shared completion-client boundary and evidence persistence helpers."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import ModelResponse


@runtime_checkable
class CompletionClient(Protocol):
    """Minimal completion interface shared by synthesis services."""

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse: ...


def persist_completion_evidence(
    *,
    repo: RepositoryProtocol,
    cost_guard: CostGuard,
    task_id: str,
    loop_id: int | None,
    agent_id: str,
    model_name: str,
    response: ModelResponse,
) -> str:
    """Persist and account for a completion not already tracked by its client."""
    if response.evidence_id is not None:
        return response.evidence_id
    evidence_id = repo.save_model_call_as_evidence(
        task_id=task_id,
        loop_id=loop_id or 0,
        agent_id=agent_id,
        provider="unknown",
        model=model_name or "unknown",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=response.usage.cost_usd,
        response_preview=response.content[:5000],
    )
    cost_guard.record_llm_usage(task_id, response.usage)
    response.evidence_id = evidence_id
    return evidence_id
