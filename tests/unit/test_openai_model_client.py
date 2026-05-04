"""Unit tests for OpenAIModelClient (PRD §11.4 + §28.2 + §28.3)."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import (
    ModelAuthError,
    ModelCallError,
)
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


def _ok_response(payload: dict[str, Any]) -> httpx.Response:
    body = {
        "choices": [
            {"message": {"content": json.dumps(payload)}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_complete_json_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response({"action": "noop"})

    client, repo = _make_client(monkeypatch, handler)
    response = await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )
    assert response.json_data == {"action": "noop"}
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 20
    # gpt-4o-mini pricing: 10/1e6 * 0.15 + 20/1e6 * 0.60
    assert response.usage.cost_usd == pytest.approx(
        10 / 1_000_000 * 0.15 + 20 / 1_000_000 * 0.60
    )
    assert response.evidence_id is not None
    snapshot = repo.get_usage_snapshot("t1")
    assert snapshot.llm_calls == 1
    # Evidence row must carry the threaded loop_id, not the legacy 0 sentinel.
    call_evidence = [
        e for e in repo._evidence.values() if e.get("type") == "model_call"
    ]
    assert len(call_evidence) == 1
    assert call_evidence[0]["loop_id"] == 1


@pytest.mark.asyncio
async def test_auth_error_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client, repo = _make_client(monkeypatch, handler)
    with pytest.raises(ModelAuthError):
        await client.complete_json(
            task_id="t1",
            loop_id=1,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            max_retries=3,
        )
    assert calls["n"] == 1
    # Auth errors are not retryable, so final-error evidence is recorded.
    error_evidence = [
        e for e in repo._evidence.values() if e.get("type") == "model_error"
    ]
    assert len(error_evidence) == 1
    assert error_evidence[0]["retryable"] is False
    # Error evidence must carry the threaded loop_id (not None).
    assert error_evidence[0]["loop_id"] == 1


@pytest.mark.asyncio
async def test_assert_within_budget_runs_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-8: SafetyStopError on the second attempt aborts the retry loop."""
    from hungerloop.services.cost_guard import SafetyStopError

    monkeypatch.setenv("HL_TEST_API_KEY", "sk-test")
    repo = InMemoryRepository()
    config = ModelConfig(
        provider=ModelProvider.OPENAI,
        model_name="gpt-4o-mini",
        api_key_env="HL_TEST_API_KEY",
    )
    pricing = PricingTable(repo)

    # Hand-rolled CostGuard stub: pass first call, raise on second.
    class _FlippingGuard:
        def __init__(self) -> None:
            self.calls = 0

        def assert_within_budget(self, task_id: str) -> None:
            self.calls += 1
            if self.calls >= 2:
                raise SafetyStopError("budget tripped between retries")

        def record_llm_usage(self, *args: object, **kwargs: object) -> None:
            return None

    guard = _FlippingGuard()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(503, json={"error": "down"})
    )

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=2.0)

    from hungerloop.services.openai_model_client import OpenAIModelClient as _Client

    client = _Client(
        config, guard, pricing, repo, client_factory=_factory  # type: ignore[arg-type]
    )
    with pytest.raises(SafetyStopError):
        await client.complete_json(
            task_id="t1",
            loop_id=2,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            max_retries=3,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
        )
    # Two assert_within_budget calls: one on first attempt (passed), one on
    # the second attempt that raised. The retry loop did not call out a third.
    assert guard.calls == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": "rate"}
            )
        return _ok_response({"action": "noop"})

    client, _ = _make_client(monkeypatch, handler)
    response = await client.complete_json(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        max_retries=2,
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
    )
    assert response.json_data == {"action": "noop"}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_server_error_retries_then_exhausts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    client, repo = _make_client(monkeypatch, handler)
    with pytest.raises(ModelCallError) as exc_info:
        await client.complete_json(
            task_id="t1",
            loop_id=1,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            max_retries=2,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
        )
    assert exc_info.value.retryable is True
    assert calls["n"] == 3
    error_evidence = [
        e for e in repo._evidence.values() if e.get("type") == "model_error"
    ]
    assert len(error_evidence) == 1
    assert error_evidence[0]["retryable"] is True


@pytest.mark.asyncio
async def test_invalid_json_response_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = {
            "choices": [{"message": {"content": "not-json{"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body)

    client, _ = _make_client(monkeypatch, handler)
    with pytest.raises(ModelCallError, match="invalid_json_response"):
        await client.complete_json(
            task_id="t1",
            loop_id=1,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            max_retries=3,
            retry_base_delay_seconds=0.0,
        )
    # Invalid JSON is non-retryable; only one HTTP call.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_json_response_not_object_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": json.dumps([1, 2, 3])}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body)

    client, _ = _make_client(monkeypatch, handler)
    with pytest.raises(ModelCallError, match="json_response_not_object"):
        await client.complete_json(
            task_id="t1",
            loop_id=1,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
        )


@pytest.mark.asyncio
async def test_missing_choices_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        )

    client, _ = _make_client(monkeypatch, handler)
    with pytest.raises(ModelCallError, match="missing_choices"):
        await client.complete_json(
            task_id="t1",
            loop_id=1,
            agent_id="execution_worker_v1",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
        )


def test_delay_for_rate_limit_honors_retry_after() -> None:
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after="2.5", attempt=0, base=10.0, cap=30.0
    )
    assert delay == 2.5


def test_delay_for_rate_limit_caps_retry_after() -> None:
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after="600", attempt=0, base=10.0, cap=20.0
    )
    assert delay == 20.0


def test_delay_for_rate_limit_falls_back_on_garbage() -> None:
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after="not-a-number", attempt=0, base=0.0, cap=0.5
    )
    assert 0.0 <= delay <= 0.5


# ---- RFC1123 HTTP-date Retry-After parsing (audit-surfaced gap) ----


def test_delay_for_rate_limit_rfc1123_future_returns_positive_delay() -> None:
    """Real OpenAI 429 sometimes returns Retry-After as an HTTP-date.
    The fallback parser must decode it and yield a delay close to the
    actual wait time, capped at `cap`."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    http_date = format_datetime(future)
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after=http_date, attempt=0, base=10.0, cap=30.0
    )
    assert 1.0 < delay <= 30.0


def test_delay_for_rate_limit_rfc1123_past_returns_zero() -> None:
    """If the server gave us a Retry-After in the past (clock skew or
    server-side bug), we should not sleep — clamp to 0.0."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after=format_datetime(past), attempt=0, base=10.0, cap=30.0
    )
    assert delay == 0.0


def test_delay_for_rate_limit_rfc1123_far_future_capped() -> None:
    """A pathological Retry-After date far in the future is capped."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    far = datetime.now(timezone.utc) + timedelta(days=1)
    delay = OpenAIModelClient._delay_for_rate_limit(
        retry_after=format_datetime(far), attempt=0, base=10.0, cap=30.0
    )
    assert delay == 30.0


def test_constructor_requires_api_key_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRepository()
    bad_config = ModelConfig(provider=ModelProvider.OPENAI, model_name="gpt-4o-mini")
    with pytest.raises(ValueError, match="api_key_env"):
        OpenAIModelClient(bad_config, CostGuard(repo), PricingTable(repo), repo)
