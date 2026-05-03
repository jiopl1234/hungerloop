"""OpenAI-compatible :class:`ModelClient` (PRD §11.2 + §28.2 + §28.3).

Wraps an HTTP call to ``/chat/completions`` with the v0.5.2 retry loop,
JSON-parse safety, evidence capture, and CostGuard pre-call check.
Errors map onto :class:`ModelCallError` / :class:`ModelAuthError` /
:class:`ModelRateLimitError` so :class:`WorkerRuntime` can route them
through the existing handlers.

Retry behaviour (PRD §11.4 + §28.2 / M6):

* 401 / 403 → :class:`ModelAuthError`, never retry.
* 429 → respect ``Retry-After`` if present, else exponential backoff +
  jitter; counts as a retry.
* 5xx / network → exponential backoff + jitter, capped at
  ``retry_max_delay_seconds``.
* JSON decode failure (PRD §28.3 / M7) → :class:`ModelCallError` with
  ``retryable=False``.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

import httpx

from hungerloop.models.usage import ModelUsage
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import (
    ModelAuthError,
    ModelCallError,
    ModelRateLimitError,
    ModelResponse,
)
from hungerloop.services.model_config import ModelConfig, PricingTable


class OpenAIModelClient:
    """OpenAI :class:`ModelClient` implementation."""

    def __init__(
        self,
        config: ModelConfig,
        cost_guard: CostGuard,
        pricing: PricingTable,
        repo: RepositoryProtocol,
        *,
        client_factory: Any = None,
    ) -> None:
        if config.api_key_env is None:
            raise ValueError("OpenAIModelClient requires config.api_key_env")
        self.config = config
        self.cost_guard = cost_guard
        self.pricing = pricing
        self.repo = repo
        self.api_key = os.environ[config.api_key_env]
        self.base_url = config.base_url or "https://api.openai.com/v1"
        # Tests inject a custom factory yielding an httpx.AsyncClient bound
        # to a transport mock; production gets the default constructor.
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=self.config.timeout_seconds)
        )

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
        """Send one chat-completion request with retries (PRD §28.2).

        I-8: ``cost_guard.assert_within_budget`` runs *before every* attempt,
        not just before the first one, so a budget that flips during a long
        retry sequence still short-circuits cleanly with ``SafetyStopError``.
        """
        last_error: ModelCallError | None = None
        async with self._client_factory() as client:
            for attempt in range(max_retries + 1):
                self.cost_guard.assert_within_budget(task_id)
                try:
                    return await self._call_once(
                        client,
                        task_id=task_id,
                        loop_id=loop_id,
                        agent_id=agent_id,
                        messages=messages,
                        max_tokens=max_tokens,
                    )
                except ModelAuthError as exc:
                    # never retry; still record final-error evidence (PRD §11.4 #6)
                    self.repo.save_model_error_as_evidence(
                        task_id=task_id,
                        loop_id=loop_id,
                        agent_id=agent_id,
                        provider=self.config.provider.value,
                        model=self.config.model_name,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        retryable=False,
                    )
                    raise
                except ModelRateLimitError as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = self._delay_for_rate_limit(
                        exc.retry_after,
                        attempt,
                        retry_base_delay_seconds,
                        retry_max_delay_seconds,
                    )
                    await asyncio.sleep(delay)
                except ModelCallError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= max_retries:
                        break
                    delay = min(
                        retry_max_delay_seconds,
                        retry_base_delay_seconds * (2**attempt)
                        + random.uniform(0, 0.5),
                    )
                    await asyncio.sleep(delay)

        assert last_error is not None
        # Final-error evidence (PRD §11.4 #6).
        self.repo.save_model_error_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            agent_id=agent_id,
            provider=self.config.provider.value,
            model=self.config.model_name,
            error_type=type(last_error).__name__,
            error_message=str(last_error),
            retryable=last_error.retryable,
        )
        raise last_error

    async def _call_once(
        self,
        client: httpx.AsyncClient,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.config.model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self.config.temperature,
                "response_format": {"type": "json_object"},
            },
        )

        if response.status_code in {401, 403}:
            raise ModelAuthError(
                "auth_error: openai credentials invalid or unauthorized"
            )
        if response.status_code == 429:
            raise ModelRateLimitError(
                "rate_limit", retry_after=response.headers.get("retry-after")
            )
        if response.status_code >= 500:
            raise ModelCallError("provider_server_error", retryable=True)
        response.raise_for_status()

        data = response.json()
        usage_raw = data.get("usage", {}) or {}
        input_tokens = int(usage_raw.get("prompt_tokens", 0))
        output_tokens = int(usage_raw.get("completion_tokens", 0))
        cost_usd = self.pricing.estimate(
            self.config.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        self.cost_guard.record_llm_usage(task_id, usage)

        evidence_id = self.repo.save_model_call_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            agent_id=agent_id,
            provider=self.config.provider.value,
            model=self.config.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            response_preview=str(data)[:5000],
        )

        choices = data.get("choices") or []
        if not choices:
            raise ModelCallError("missing_choices", retryable=False)
        content = choices[0].get("message", {}).get("content", "")
        try:
            json_data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelCallError(
                f"invalid_json_response: {exc.msg}",
                retryable=False,
            ) from exc

        if not isinstance(json_data, dict):
            raise ModelCallError(
                f"json_response_not_object: type={type(json_data).__name__}",
                retryable=False,
            )

        return ModelResponse(
            content=content,
            json_data=json_data,
            usage=usage,
            evidence_id=evidence_id,
        )

    @staticmethod
    def _delay_for_rate_limit(
        retry_after: str | None,
        attempt: int,
        base: float,
        cap: float,
    ) -> float:
        """Pick a sleep duration honouring ``Retry-After`` when present."""
        if retry_after:
            try:
                return min(cap, float(retry_after))
            except ValueError:
                pass
        return float(min(cap, base * (2**attempt) + random.uniform(0, 0.5)))
