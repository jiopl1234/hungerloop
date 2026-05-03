"""Unit tests for ModelClient types and DummyModelClient (PRD §11 / §28.1)."""
from __future__ import annotations

from hungerloop.models.usage import ModelUsage
from hungerloop.services.model_client import (
    DummyModelClient,
    ModelAuthError,
    ModelCallError,
    ModelRateLimitError,
    ModelResponse,
)


def test_model_call_error_default_not_retryable() -> None:
    err = ModelCallError("boom")
    assert err.retryable is False
    assert str(err) == "boom"


def test_model_call_error_retryable_flag() -> None:
    err = ModelCallError("server_error", retryable=True)
    assert err.retryable is True


def test_model_auth_error_is_never_retryable() -> None:
    err = ModelAuthError("401")
    assert isinstance(err, ModelCallError)
    assert err.retryable is False


def test_model_rate_limit_error_carries_retry_after() -> None:
    err = ModelRateLimitError("429", retry_after="2")
    assert err.retryable is True
    assert err.retry_after == "2"


async def test_dummy_returns_fallback_when_script_empty() -> None:
    client = DummyModelClient()
    response = await client.complete_json(
        task_id="t1",
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
    )
    assert response.json_data == {"summary": "dummy fallback", "actions": []}
    assert response.usage.input_tokens == 1
    assert client.call_count == 1


async def test_dummy_consumes_script_in_order() -> None:
    first = ModelResponse(
        content='{"summary":"a","actions":[]}',
        json_data={"summary": "a", "actions": []},
        usage=ModelUsage(input_tokens=2, output_tokens=3),
    )
    second = ModelResponse(
        content='{"summary":"b","actions":[]}',
        json_data={"summary": "b", "actions": []},
        usage=ModelUsage(),
    )
    client = DummyModelClient([first, second])

    r1 = await client.complete_json(
        task_id="t1", agent_id="a1", messages=[], max_tokens=10
    )
    r2 = await client.complete_json(
        task_id="t1", agent_id="a1", messages=[], max_tokens=10
    )
    r3 = await client.complete_json(
        task_id="t1", agent_id="a1", messages=[], max_tokens=10
    )

    assert r1.json_data == {"summary": "a", "actions": []}
    assert r2.json_data == {"summary": "b", "actions": []}
    assert r3.json_data == {"summary": "dummy fallback", "actions": []}
    assert client.call_count == 3


async def test_dummy_with_actions_emits_them() -> None:
    actions = [
        {"tool_name": "write_file", "args": {"path": "report.md", "content": "hi"}}
    ]
    client = DummyModelClient.with_actions(actions)
    response = await client.complete_json(
        task_id="t1", agent_id="a1", messages=[], max_tokens=10
    )
    assert response.json_data is not None
    assert response.json_data["actions"] == actions
    assert response.json_data["summary"] == "scripted dummy response"


async def test_dummy_ignores_retry_kwargs() -> None:
    """Retry kwargs are part of the protocol; dummy should accept and ignore."""
    client = DummyModelClient()
    response = await client.complete_json(
        task_id="t1",
        agent_id="a1",
        messages=[],
        max_tokens=10,
        max_retries=5,
        retry_base_delay_seconds=0.1,
        retry_max_delay_seconds=2.0,
    )
    assert response.json_data == {"summary": "dummy fallback", "actions": []}


def test_constructor_copies_input_list() -> None:
    seed = [
        ModelResponse(content="{}", json_data={}, usage=ModelUsage()),
    ]
    client = DummyModelClient(seed)
    seed.clear()
    assert len(client._script) == 1  # noqa: SLF001 — internal-state assertion
