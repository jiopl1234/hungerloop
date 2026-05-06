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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from hungerloop.models.events import EventType
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

# Default ratio for the "actual usage diverged from estimate" alarm
# (PRD §8.7.1). Operators can tighten via env var per deployment; the
# 0.20 default is loose enough that gpt-4o-mini noise won't spam events
# but tight enough that a 50% under-count of long prompts shows up.
_COST_DELTA_THRESHOLD_DEFAULT = 0.20

# Crude character → token heuristic used for the pre-call estimate. Real
# tokenisation needs tiktoken (heavy dep); 4 chars/token is the well-known
# rule-of-thumb for English-heavy chat. The reconciliation event exists
# precisely so ops can see when this heuristic is wrong.
_CHARS_PER_TOKEN = 4


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
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ModelAuthError(
                f"Environment variable {config.api_key_env} is not set"
            )
        self.api_key = api_key
        self.base_url = config.base_url or "https://api.openai.com/v1"
        # Tests inject a custom factory yielding an httpx.AsyncClient bound
        # to a transport mock; production gets the default constructor.
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=self.config.timeout_seconds)
        )
        # Resolved once per client instance (PRD §8.7.1) so a single call
        # never sees a half-changed env. Bad parses fall back to the
        # default rather than crashing — the operator's env is observable
        # via the emitted event payload anyway.
        try:
            self._cost_delta_threshold = float(
                os.environ.get(
                    "HUNGERLOOP_COST_DELTA_THRESHOLD",
                    str(_COST_DELTA_THRESHOLD_DEFAULT),
                )
            )
        except ValueError:
            self._cost_delta_threshold = _COST_DELTA_THRESHOLD_DEFAULT

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

        v0.5d.0 (PRD §7.5): emits MODEL_CALL_STARTED once per call,
        plus _SUCCEEDED / _FAILED / _AUTH_REQUIRED / _RATE_LIMITED
        per attempt. The orchestrator's per-loop sequence shows every
        retry; downstream aggregators can collapse by ``call_id``.
        """
        # Stable per-call id so retries on the same call can be collapsed
        # by downstream aggregators / dashboards.
        call_id = f"mc-{loop_id:03d}-{agent_id}-{id(messages):x}"
        self.repo.append_event(
            EventType.MODEL_CALL_STARTED,
            {
                "call_id": call_id,
                "agent_id": agent_id,
                "model": self.config.model_name,
                "max_tokens": max_tokens,
                "max_retries": max_retries,
            },
            task_id=task_id,
            loop_id=loop_id,
        )
        last_error: ModelCallError | None = None
        async with self._client_factory() as client:
            for attempt in range(max_retries + 1):
                self.cost_guard.assert_within_budget(task_id)
                try:
                    response = await self._call_once(
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
                    self.repo.append_event(
                        EventType.MODEL_AUTH_REQUIRED,
                        {
                            "call_id": call_id,
                            "agent_id": agent_id,
                            "model": self.config.model_name,
                            "attempt": attempt,
                        },
                        task_id=task_id,
                        loop_id=loop_id,
                    )
                    raise
                except ModelRateLimitError as exc:
                    last_error = exc
                    self.repo.append_event(
                        EventType.MODEL_RATE_LIMITED,
                        {
                            "call_id": call_id,
                            "agent_id": agent_id,
                            "model": self.config.model_name,
                            "attempt": attempt,
                            "retry_after": exc.retry_after,
                        },
                        task_id=task_id,
                        loop_id=loop_id,
                    )
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
                else:
                    self.repo.append_event(
                        EventType.MODEL_CALL_SUCCEEDED,
                        {
                            "call_id": call_id,
                            "agent_id": agent_id,
                            "model": self.config.model_name,
                            "attempt": attempt,
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "cost_usd": response.usage.cost_usd,
                        },
                        task_id=task_id,
                        loop_id=loop_id,
                    )
                    return response

        assert last_error is not None
        # Final MODEL_CALL_FAILED event after all retries exhausted.
        self.repo.append_event(
            EventType.MODEL_CALL_FAILED,
            {
                "call_id": call_id,
                "agent_id": agent_id,
                "model": self.config.model_name,
                "error_type": type(last_error).__name__,
                "error_message": str(last_error)[:240],
                "retryable": last_error.retryable,
            },
            task_id=task_id,
            loop_id=loop_id,
        )
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
        try:
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
        except httpx.TransportError as exc:
            raise ModelCallError(
                f"network_error:{type(exc).__name__}: {exc}", retryable=True
            ) from exc

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
        if response.status_code >= 400:
            raise ModelCallError(
                f"provider_http_error:{response.status_code}", retryable=False
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelCallError(
                "invalid_provider_json_response", retryable=False
            ) from exc

        if not isinstance(data, dict):
            raise ModelCallError(
                f"provider_json_not_object: type={type(data).__name__}",
                retryable=False,
            )
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
        # Persist successful-call evidence before the post-call budget
        # check. If actual usage trips SAFETY_STOP, the stop report and
        # trace still have the model_call row for forensics.
        self.cost_guard.record_llm_usage(task_id, usage)
        self._maybe_emit_reconciliation(
            task_id=task_id,
            loop_id=loop_id,
            messages=messages,
            max_tokens=max_tokens,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            actual_cost_usd=cost_usd,
        )

        choices = data.get("choices") or []
        if not choices:
            raise ModelCallError("missing_choices", retryable=False)
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        message = first_choice.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(content, str):
            raise ModelCallError("invalid_content_type", retryable=False)

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

    def _maybe_emit_reconciliation(
        self,
        *,
        task_id: str,
        loop_id: int,
        messages: list[dict[str, str]],
        max_tokens: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_cost_usd: float,
    ) -> None:
        """Emit a ``cost_reconciliation`` event when usage drifts past
        the configured threshold (PRD §8.7.1).

        Skipped when the model is unknown to PricingTable: that case is
        already covered by the ``unknown_model_pricing`` event and a
        second warning would just be noise.
        """
        if self.config.model_name not in PricingTable.PRICES:
            return

        estimated_input_tokens = self._estimate_prompt_tokens(messages)
        # The output cap is the only honest pre-call upper bound we have
        # without a real tokenizer. Real outputs are usually well below
        # this; ops who care can run a tiktoken-aware variant downstream.
        estimated_output_tokens = max_tokens
        estimated_total = estimated_input_tokens + estimated_output_tokens
        actual_total = actual_input_tokens + actual_output_tokens
        if estimated_total <= 0:
            return
        delta_ratio = abs(actual_total - estimated_total) / estimated_total
        if delta_ratio <= self._cost_delta_threshold:
            return

        estimated_cost_usd = self.pricing.estimate(
            self.config.model_name,
            input_tokens=estimated_input_tokens,
            output_tokens=estimated_output_tokens,
        )
        self.repo.append_event(
            EventType.COST_RECONCILIATION,
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "model": self.config.model_name,
                "estimated_tokens": estimated_total,
                "actual_tokens": actual_total,
                "estimated_cost_usd": round(estimated_cost_usd, 6),
                "actual_cost_usd": round(actual_cost_usd, 6),
                "delta_ratio": round(delta_ratio, 3),
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    @staticmethod
    def _estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
        """Crude pre-call token estimate (4 chars/token rule of thumb).

        Real tokenisation requires tiktoken; we deliberately keep the
        runtime dependency-free. The reconciliation event surfaces cases
        where this heuristic is far off, which is the whole point.
        """
        total_chars = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
        return max(1, total_chars // _CHARS_PER_TOKEN)

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
                try:
                    retry_time = parsedate_to_datetime(retry_after)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=timezone.utc)
                    seconds = (
                        retry_time - datetime.now(timezone.utc)
                    ).total_seconds()
                    return min(cap, max(0.0, seconds))
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(cap, base * (2**attempt) + random.uniform(0, 0.5)))
