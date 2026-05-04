"""Cost-reconciliation event tests (PRD §8.7.1).

When real LLM usage diverges from the pre-call estimate by more than
``HUNGERLOOP_COST_DELTA_THRESHOLD`` (default 0.20), the OpenAIModelClient
emits a ``cost_reconciliation`` event. Pure observability — BudgetGuard
still records the *actual* numbers, never the estimate.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_config import ModelConfig, ModelProvider, PricingTable
from hungerloop.services.openai_model_client import OpenAIModelClient


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    model_name: str = "gpt-4o-mini",
) -> tuple[OpenAIModelClient, InMemoryRepository]:
    monkeypatch.setenv("HL_TEST_API_KEY", "sk-test")
    repo = InMemoryRepository()
    config = ModelConfig(
        provider=ModelProvider.OPENAI,
        model_name=model_name,
        api_key_env="HL_TEST_API_KEY",
    )
    cost_guard = CostGuard(repo)
    pricing = PricingTable(repo)
    transport = httpx.MockTransport(handler)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    client = OpenAIModelClient(
        config, cost_guard, pricing, repo, client_factory=_factory
    )
    return client, repo


def _response_with_tokens(prompt: int, completion: int) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": json.dumps({"action": "noop"})}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    return httpx.Response(200, json=body)


def _recon_events(repo: InMemoryRepository) -> list[dict[str, Any]]:
    return [
        e for e in repo._events if e["event_type"] == "cost_reconciliation"
    ]


# ---------------------------------------------------------------------------
# Threshold behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_within_threshold_does_not_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimate ≈ actual → no event."""
    # Default heuristic: 4 chars/token. We send a 4000-char prompt so
    # estimated_input ≈ 1000; max_tokens=200 so estimated_total ≈ 1200.
    # Actual prompt+completion = 1100 → delta ≈ 8% (well below 20%).
    monkeypatch.delenv("HUNGERLOOP_COST_DELTA_THRESHOLD", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return _response_with_tokens(prompt=1000, completion=100)

    client, repo = _make_client(monkeypatch, handler)
    await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "x" * 4000}],
        max_tokens=200,
    )
    assert _recon_events(repo) == []


@pytest.mark.asyncio
async def test_over_threshold_emits_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimate ≪ actual → event with delta_ratio."""
    monkeypatch.delenv("HUNGERLOOP_COST_DELTA_THRESHOLD", raising=False)
    # Prompt is 400 chars → estimated_input ≈ 100; max_tokens=100 →
    # estimated_total ≈ 200. Actual total = 1000+500 = 1500 → delta ≈ 6.5x.
    def handler(_: httpx.Request) -> httpx.Response:
        return _response_with_tokens(prompt=1000, completion=500)

    client, repo = _make_client(monkeypatch, handler)
    await client.complete_json(
        task_id="t1",
        loop_id=2,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "x" * 400}],
        max_tokens=100,
    )
    events = _recon_events(repo)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["actual_tokens"] == 1500
    assert payload["estimated_tokens"] == 100 + 100
    # 1300 / 200 = 6.5
    assert payload["delta_ratio"] == pytest.approx(6.5, rel=1e-3)
    assert payload["task_id"] == "t1"
    assert payload["loop_id"] == 2


@pytest.mark.asyncio
async def test_threshold_overridable_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowering the threshold makes the same call emit an event."""
    monkeypatch.setenv("HUNGERLOOP_COST_DELTA_THRESHOLD", "0.05")

    def handler(_: httpx.Request) -> httpx.Response:
        # Same payload as test_within_threshold_does_not_emit.
        return _response_with_tokens(prompt=1000, completion=100)

    client, repo = _make_client(monkeypatch, handler)
    await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "x" * 4000}],
        max_tokens=200,
    )
    # Estimated 1200 vs actual 1100 → 8.3% > 5% threshold.
    assert len(_recon_events(repo)) == 1


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_estimate_skips_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown model (PricingTable returns 0) → no double-warn.

    We still expect the ``unknown_model_pricing`` event from the cost
    estimation step; reconciliation just shouldn't *additionally* fire.
    """
    monkeypatch.delenv("HUNGERLOOP_COST_DELTA_THRESHOLD", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return _response_with_tokens(prompt=999, completion=999)

    client, repo = _make_client(
        monkeypatch, handler, model_name="ghost-model-9000"
    )
    await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "tiny"}],
        max_tokens=4,
    )
    assert _recon_events(repo) == []
    # Sanity: the unknown-pricing path *did* fire.
    assert any(
        e["event_type"] == "unknown_model_pricing" for e in repo._events
    )


# ---------------------------------------------------------------------------
# BudgetGuard contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_guard_records_actual_not_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when reconciliation fires, BudgetGuard sees real numbers."""
    monkeypatch.delenv("HUNGERLOOP_COST_DELTA_THRESHOLD", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return _response_with_tokens(prompt=1000, completion=500)

    client, repo = _make_client(monkeypatch, handler)
    await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "x" * 400}],
        max_tokens=100,
    )
    snapshot = repo.get_usage_snapshot("t1")
    # Actual prompt+completion=1500; cost = 1000/1e6*0.15 + 500/1e6*0.60.
    assert snapshot.tokens == 1500
    assert snapshot.cost_usd == pytest.approx(
        1000 / 1_000_000 * 0.15 + 500 / 1_000_000 * 0.60
    )
    # And reconciliation still fired.
    assert len(_recon_events(repo)) == 1
