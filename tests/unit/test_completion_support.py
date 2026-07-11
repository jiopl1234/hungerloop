"""Tests for shared synthesis completion support."""
from __future__ import annotations

from hungerloop.models.usage import ModelUsage
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.completion_support import persist_completion_evidence
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import ModelResponse


def test_persist_completion_evidence_is_idempotent_and_accounts_once() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "goal")
    response = ModelResponse(
        content='{"decisions": []}',
        usage=ModelUsage(input_tokens=3, output_tokens=2, cost_usd=0.25),
    )
    guard = CostGuard(repo)

    first = persist_completion_evidence(
        repo=repo,
        cost_guard=guard,
        task_id="t1",
        loop_id=None,
        agent_id="spec_entailment_auditor",
        model_name="test-model",
        response=response,
    )
    second = persist_completion_evidence(
        repo=repo,
        cost_guard=guard,
        task_id="t1",
        loop_id=None,
        agent_id="spec_entailment_auditor",
        model_name="test-model",
        response=response,
    )

    assert second == first
    evidence = repo.list_evidence("t1", evidence_type="model_call")
    assert len(evidence) == 1
    assert evidence[0]["loop_id"] == 0
    assert evidence[0]["response_preview"] == response.content
    assert repo.get_usage_snapshot("t1").tokens == 5
    assert repo.get_hunger_clock("t1").consumed_tokens == 5
