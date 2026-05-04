"""Model client protocol, response model, errors, and DummyModelClient.

This module defines the *only* surface a Worker uses to talk to an LLM.
v0.5a does not ship :class:`OpenAIModelClient` — that is Day 9 (PRD §26).
What ships here:

* :class:`ModelResponse` — structured response (PRD §11.1).
* :class:`ModelClient` Protocol — async ``complete_json`` with retry params
  threaded through from :class:`BudgetAllocation` per PRD §28.2 / M6.
* :class:`ModelCallError`, :class:`ModelAuthError`, :class:`ModelRateLimitError` —
  exception hierarchy mapped onto WorkerResult.error_type by
  :class:`~hungerloop.services.worker_runtime.WorkerRuntime` (PRD §11.4).
* :class:`DummyModelClient` — deterministic, scriptable client used by
  tests and demos (PRD §11.5 + §28.1 / M21). It is **not** a fallback for
  production: the demo loader injects scripted responses; production paths
  use the real :class:`ModelClient` implementation.
"""
from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, Field

from hungerloop.models.usage import ModelUsage


class ModelResponse(BaseModel):
    """Structured model response carried back to the Worker (PRD §11.1)."""

    content: str
    json_data: dict[str, object] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    evidence_id: str | None = None


class ModelCallError(Exception):
    """Base error for model client failures.

    ``retryable=True`` allows :class:`ModelClient` retry loops to back off
    and try again; the WorkerRuntime mirrors this onto
    ``WorkerResult.retryable`` (PRD §11.4).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable: bool = retryable


class ModelAuthError(ModelCallError):
    """401 / 403 from the provider.

    Always non-retryable; WorkerRuntime maps to ``requires_human=True`` so
    the Orchestrator can surface ``StopReason.HUMAN_REQUIRED`` (PRD §11.4
    item 1).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ModelRateLimitError(ModelCallError):
    """429 from the provider; carries the raw ``Retry-After`` header.

    ``retry_after`` may be a seconds string ("0.5") or an HTTP-date string;
    interpretation belongs to the retry loop (PRD §28.2).
    """

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after: str | None = retry_after


class ModelClient(Protocol):
    """Single async surface used by Workers (PRD §11.1 / §28.2 / M6).

    Retry parameters are kwargs (defaults make non-retrying behaviour the
    norm); callers sourced from :class:`ContextPack.budget` thread these
    through every call so :class:`OpenAIModelClient` can run its retry loop
    without reaching back into the Worker layer.
    """

    async def complete_json(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        max_retries: int = 0,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 20.0,
    ) -> ModelResponse: ...


class DummyModelClient:
    """Deterministic, scriptable client for tests and demos (PRD §11.5 / §28.1).

    Construct with a list of pre-baked :class:`ModelResponse` objects; each
    ``complete_json`` call pops the next one in FIFO order. Once the script
    is exhausted, the client returns an empty fallback response with
    ``actions=[]`` so loops can settle into stagnation rather than crashing.

    The classmethod :meth:`with_actions` is the demo entry point — it builds
    a single-shot client whose only response carries the supplied tool
    actions. CLI loaders use this to wire ``examples/demo_dummy_script.py``
    into a run.

    The retry kwargs are accepted but ignored — the dummy never fails.
    """

    def __init__(self, scripted_responses: list[ModelResponse] | None = None) -> None:
        self._script: list[ModelResponse] = list(scripted_responses or [])
        self._calls: int = 0

    # ----- FROZEN ACTION SCHEMA (v0.5b.1+) -----
    # Each action is a dict with at least:
    #   tool_name: str   - matches a Tool registered in tool_harness
    #   args: dict       - tool-specific args (any JSON-serializable shape)
    # Optional keys:
    #   delay_ms: int    - test scaffolding for timing assertions
    # Adding new optional keys is allowed across minor releases.
    # Removing or renaming any of the above requires a major-version bump
    # on this schema and a coordinated test migration.
    # -------------------------------------------
    @classmethod
    def with_actions(cls, actions: list[dict[str, object]]) -> DummyModelClient:
        """Build a dummy client with one scripted response carrying ``actions``.

        Args:
            actions: List of tool action dicts to embed in the JSON payload.
                Each dict typically has ``tool_name`` and ``args`` keys; the
                full schema is the Worker's responsibility.

        Returns:
            A new :class:`DummyModelClient` with exactly one scripted call.
        """
        payload: dict[str, object] = {
            "summary": "scripted dummy response",
            "actions": list(actions),
        }
        response = ModelResponse(
            content=json.dumps(payload),
            json_data=payload,
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        )
        return cls([response])

    @property
    def call_count(self) -> int:
        """Number of ``complete_json`` invocations served (test introspection)."""
        return self._calls

    async def complete_json(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        max_retries: int = 0,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 20.0,
    ) -> ModelResponse:
        self._calls += 1
        if self._script:
            return self._script.pop(0)
        empty: dict[str, object] = {"summary": "dummy fallback", "actions": []}
        return ModelResponse(
            content=json.dumps(empty),
            json_data=empty,
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        )
