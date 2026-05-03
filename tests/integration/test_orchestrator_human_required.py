"""HUMAN_REQUIRED path: model auth error propagates (PRD §23.2 + §28.5)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.model_config import ModelConfig, ModelProvider, PricingTable
from hungerloop.services.openai_model_client import OpenAIModelClient
from tests.integration.conftest import make_seed_report_task, workspace


def _always_401(_: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": "unauthorized"})


async def test_auth_error_becomes_human_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HL_INT_API_KEY", "sk-bad")
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    transport = httpx.MockTransport(_always_401)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=2.0)

    config = ModelConfig(
        provider=ModelProvider.OPENAI,
        model_name="gpt-4o-mini",
        api_key_env="HL_INT_API_KEY",
    )
    client = OpenAIModelClient(
        config,
        CostGuard(repo),
        PricingTable(repo),
        repo,
        client_factory=_factory,
    )

    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=client,
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.HUMAN_REQUIRED
    assert report.goal_status == "paused"
    # MODEL_ERROR evidence was recorded by the client on the auth failure.
    assert any(
        e.get("type") == "model_error" and e.get("error_type") == "ModelAuthError"
        for e in repo._evidence.values()
    )
