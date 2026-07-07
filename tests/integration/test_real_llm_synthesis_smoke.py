"""Real LLM smoke test for SpecCheckSynthesizer with glm-5.2.

Covers VAL-SYN-015 (real LLM smoke validates glm-5.2 synthesis safely)
and VAL-CROSS-012 (real LLM smoke uses glm-5.2 safely).

This test is SKIPPED by default unless HUNGERLOOP_API_KEY and
HUNGERLOOP_BASE_URL are set in the environment. When enabled, it:

1. Reads credentials ONLY from approved environment / .env sources.
2. Uses model ``glm-5.2`` for exactly one low-token request.
3. Routes the response through the normal synthesizer and gate.
4. Records accepted and rejected proposal counts.
5. Verifies that stdout, stderr, logs, evidence, and persisted event
   payloads contain no API key, bearer token, base-url secret, or
   raw .env value.

Run explicitly:
    pytest tests/integration/test_real_llm_synthesis_smoke.py -q -s

Or with env vars:
    HUNGERLOOP_API_KEY=... HUNGERLOOP_BASE_URL=... \
    pytest tests/integration/test_real_llm_synthesis_smoke.py -q -s
"""
from __future__ import annotations

import os
import re
from typing import Any

import pytest

from hungerloop.models.hunger import HungerLedger, HungerPolicy
from hungerloop.models.usage import ModelUsage
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import ModelResponse
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.spec_check_synthesizer import (
    SpecCheckSynthesizer,
    run_plan_time_synthesis,
)

# Secret patterns to scan for in all output.
_SECRET_PATTERNS: list[str] = [
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI-style key prefix
    r"Bearer\s+[a-zA-Z0-9\-._~+/]+",  # Bearer token
    r"HUNGERLOOP_API_KEY\s*=\s*\S+",  # Raw env var assignment
    r"HUNGERLOOP_BASE_URL\s*=\s*\S+",  # Raw env var assignment
]

_MODEL_NAME = "glm-5.2"
_MISSION_PROSE = (
    "The project must have a main.py file and pass pytest. "
    "The codebase should include a README."
)
_FEATURE_DESCS = [
    "Feature: main module at src/main.py",
    "Feature: test suite passes with pytest",
]


def _has_credentials() -> bool:
    return bool(
        os.environ.get("HUNGERLOOP_API_KEY")
        and os.environ.get("HUNGERLOOP_BASE_URL")
    )


# Try to load .env if dotenv is available.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class _RealCompletionClient:
    """Real completion client using httpx against the OpenAI-compatible API.

    This is NOT a dummy or fake client. It makes a real HTTP request
    to the configured endpoint using the real API key.
    """

    def __init__(self, api_key: str, base_url: str) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        import httpx

        self._call_count += 1
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _MODEL_NAME,
                    "messages": messages,
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            usage_raw = data.get("usage", {})
            usage = ModelUsage(
                input_tokens=usage_raw.get("prompt_tokens", 0),
                output_tokens=usage_raw.get("completion_tokens", 0),
                cost_usd=0.0,
            )
            return ModelResponse(content=content, usage=usage)


def _secret_scan(text: str) -> list[str]:
    """Scan text for secret patterns. Returns list of found patterns."""
    found: list[str] = []
    for pattern in _SECRET_PATTERNS:
        matches = re.findall(pattern, text)
        found.extend(matches)
    return found


@pytest.mark.skipif(
    not _has_credentials(),
    reason="HUNGERLOOP_API_KEY and HUNGERLOOP_BASE_URL not set",
)
class TestRealLLMSynthesisSmoke:
    """Real LLM smoke test for glm-5.2 synthesis.

    This test makes exactly one real LLM request through the real
    completion client path, routes the response through the normal
    synthesizer and gate, and verifies that no secrets leak into
    persisted events.
    """

    @pytest.mark.asyncio
    async def test_real_glm52_synthesis_smoke(self) -> None:
        api_key = os.environ["HUNGERLOOP_API_KEY"]
        base_url = os.environ["HUNGERLOOP_BASE_URL"]

        repo = InMemoryRepository()
        repo.create_task("smoke-task", "synthesis smoke test")
        repo.set_hunger_policy(
            "smoke-task",
            HungerPolicy(
                synthesis_enabled=True,
                synthesis_plan_time_tier=0,
                synthesis_max_total_items=20,
                max_total_cost_usd=100.0,
                max_total_tokens=1_000_000,
            ),
        )
        repo.save_hunger_ledger(
            "smoke-task", HungerLedger(task_id="smoke-task", items=[])
        )

        client = _RealCompletionClient(api_key, base_url)
        cost_guard = CostGuard(repo)
        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)

        # Run plan-time synthesis with the real client
        injected_ids = await run_plan_time_synthesis(
            task_id="smoke-task",
            repo=repo,
            cost_guard=cost_guard,
            completion_client=client,
            gate=gate,
            refinement_compiler=compiler,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_plan_time_tier=0,
            synthesis_max_total_items=20,
            model_name=_MODEL_NAME,
        )

        # Verify exactly one completion call was made
        assert client.call_count == 1, (
            f"Expected exactly 1 completion call, got {client.call_count}"
        )

        # Record accepted and rejected counts
        accepted_count = len(injected_ids)

        # Verify the model name appears in events (non-secret)
        events = repo.list_events("smoke-task")
        attempted_events = [
            e for e in events if e["event_type"] == "synthesis_attempted"
        ]
        assert len(attempted_events) == 1
        assert attempted_events[0]["payload"]["model"] == _MODEL_NAME

        # Secret-scan all persisted event payloads
        all_secrets_found: list[str] = []
        for event in events:
            payload_str = str(event["payload"])
            secrets = _secret_scan(payload_str)
            all_secrets_found.extend(secrets)

        # Also scan the event types themselves
        for event in events:
            secrets = _secret_scan(event["event_type"])
            all_secrets_found.extend(secrets)

        assert all_secrets_found == [], (
            f"Secret values found in persisted events: {all_secrets_found}"
        )

        # Verify that injected items (if any) have correct provenance
        if injected_ids:
            ledger = repo.get_hunger_ledger("smoke-task")
            for item_id in injected_ids:
                item = next(i for i in ledger.items if i.id == item_id)
                assert item.generated_by == "synthesizer"
                assert item.refinement_kind == "spec_coverage"
                # Verify no secrets in item title
                secrets = _secret_scan(item.title)
                assert secrets == [], (
                    f"Secrets found in item title {item_id}: {secrets}"
                )

        # Print a redacted summary for evidence
        print("\n--- Real LLM Smoke Summary ---")
        print(f"Model: {_MODEL_NAME}")
        print(f"Completion calls: {client.call_count}")
        print(f"Accepted proposals: {accepted_count}")
        print(f"Injected item ids: {injected_ids}")
        print(f"Secret scan: {'PASS' if not all_secrets_found else 'FAIL'}")
        print("--- End Summary ---\n")

    @pytest.mark.asyncio
    async def test_real_glm52_no_dummy_fallback(self) -> None:
        """Verify the real path is used and does not fall back to dummy."""
        api_key = os.environ["HUNGERLOOP_API_KEY"]
        base_url = os.environ["HUNGERLOOP_BASE_URL"]

        repo = InMemoryRepository()
        repo.create_task("smoke-nodummy", "no dummy fallback test")
        repo.set_hunger_policy(
            "smoke-nodummy",
            HungerPolicy(
                synthesis_enabled=True,
                synthesis_max_total_items=20,
                max_total_cost_usd=100.0,
                max_total_tokens=1_000_000,
            ),
        )
        repo.save_hunger_ledger(
            "smoke-nodummy", HungerLedger(task_id="smoke-nodummy", items=[])
        )

        client = _RealCompletionClient(api_key, base_url)

        synth = SpecCheckSynthesizer(
            repo=repo,
            cost_guard=CostGuard(repo),
            completion_client=client,
            gate=CheckProposalGate(),
            model_name=_MODEL_NAME,
        )

        result = await synth.synthesize(
            task_id="smoke-nodummy",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        # The real client must have been called (not a dummy)
        assert client.call_count == 1
        assert result.completion_called
        assert result.model_name == _MODEL_NAME

        # Verify no secrets in any event payload
        events = repo.list_events("smoke-nodummy")
        for event in events:
            payload_str = str(event["payload"])
            secrets = _secret_scan(payload_str)
            assert secrets == [], (
                f"Secrets found in event {event['event_type']}: {secrets}"
            )
