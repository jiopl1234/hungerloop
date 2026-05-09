"""Opt-in real-LLM smoke for v0.5f loop memory."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import AcceptanceCheckType, StopReason
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_client import ModelResponse
from hungerloop.services.model_config import ModelConfig, ModelProvider, PricingTable
from hungerloop.services.openai_model_client import OpenAIModelClient


class _CapturingOpenAIClient(OpenAIModelClient):
    """Record real prompts/actions while preserving OpenAIModelClient behavior."""

    def __init__(
        self,
        config: ModelConfig,
        cost_guard: CostGuard,
        pricing: PricingTable,
        repo: InMemoryRepository,
    ) -> None:
        super().__init__(config, cost_guard, pricing, repo)
        self.prompts: list[str] = []
        self.actions_by_loop: dict[int, set[tuple[str, str]]] = {}

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
        self.prompts.append(messages[1]["content"])
        response = await super().complete_json(
            task_id=task_id,
            loop_id=loop_id,
            agent_id=agent_id,
            messages=messages,
            max_tokens=max_tokens,
            max_retries=max_retries,
            retry_base_delay_seconds=retry_base_delay_seconds,
            retry_max_delay_seconds=retry_max_delay_seconds,
        )
        self.actions_by_loop[loop_id] = _action_pairs(response)
        return response


def _action_pairs(response: ModelResponse) -> set[tuple[str, str]]:
    data = response.json_data or {}
    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list):
        return set()

    pairs: set[tuple[str, str]] = set()
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        tool_name = raw.get("tool_name")
        args = raw.get("args")
        if isinstance(tool_name, str):
            pairs.add((tool_name, repr(args)))
    return pairs


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required when RUN_REAL_LLM=1")
    return value


def _seed_real_smoke_task(repo: InMemoryRepository) -> None:
    repo.create_task(
        "t1",
        (
            "Create fizzbuzz.py with a fizzbuzz(n) function. It must return "
            "the input number as a string for ordinary values, 'Fizz' for "
            "multiples of 3, 'Buzz' for multiples of 5, and 'FizzBuzz' for "
            "multiples of both 3 and 5."
        ),
    )
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=1.0,
            max_total_tokens=40_000,
            initial_hunger=100.0,
            decay_duration_seconds=12.0,
        ),
    )
    repo.get_hunger_clock("t1")
    item = HungerItem(
        id="H-001",
        title="implement fizzbuzz",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                params={
                    "argv": [
                        "python",
                        "-c",
                        (
                            "from fizzbuzz import fizzbuzz; "
                            "assert fizzbuzz(1) == '1'; "
                            "assert fizzbuzz(3) == 'Fizz'; "
                            "assert fizzbuzz(5) == 'Buzz'; "
                            "assert fizzbuzz(15) == 'FizzBuzz'"
                        ),
                    ],
                    "timeout": 5,
                },
                description="fizzbuzz.py passes edge-case smoke tests",
            )
        ],
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_LLM") != "1",
    reason="real-LLM smoke; set RUN_REAL_LLM=1 to enable",
)
async def test_real_llm_does_not_repeat_prior_actions(tmp_path: Path) -> None:
    """Run a bounded real-model smoke and check rejected-loop action drift.

    Required env when enabled:
    - ``HUNGERLOOP_REAL_LLM_API_KEY``
    - optional ``HUNGERLOOP_REAL_LLM_BASE_URL``
    - optional ``HUNGERLOOP_REAL_LLM_MODEL`` (defaults to ``gpt-4o-mini``)

    Expected cost is roughly 1k-4k tokens for common OpenAI-compatible
    gateways; the orchestrator is still capped at six loops.
    """
    _require_env("HUNGERLOOP_REAL_LLM_API_KEY")
    repo = InMemoryRepository()
    _seed_real_smoke_task(repo)

    config = ModelConfig(
        provider=ModelProvider.OPENAI,
        model_name=os.environ.get("HUNGERLOOP_REAL_LLM_MODEL", "gpt-4o-mini"),
        api_key_env="HUNGERLOOP_REAL_LLM_API_KEY",
        base_url=os.environ.get("HUNGERLOOP_REAL_LLM_BASE_URL"),
        timeout_seconds=60,
        max_tokens=1200,
        temperature=0.1,
    )
    client = _CapturingOpenAIClient(
        config,
        CostGuard(repo),
        PricingTable(repo),
        repo,
    )
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=tmp_path,
        model_client=client,
        max_loops_safety_cap=6,
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")

    assert report.stop_reason is StopReason.DONE
    rejected_loops = [
        trace.loop_id
        for trace in repo.list_loop_traces("t1")
        if not trace.committed and trace.loop_id + 1 in client.actions_by_loop
    ]
    for loop_id in rejected_loops:
        assert client.actions_by_loop[loop_id] != client.actions_by_loop[loop_id + 1]
    if rejected_loops:
        assert any("Prior loop context" in prompt for prompt in client.prompts[1:])
