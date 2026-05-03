"""SAFETY_STOP path: cost ceiling triggers immediate stop (PRD §23.2)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from tests.integration.conftest import make_seed_report_task, workspace


async def test_cost_ceiling_blocks_first_loop(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_seed_report_task()(repo)
    # Pre-blow the budget: clock already over the cost ceiling before any loop.
    policy = repo.get_hunger_policy("t1")
    policy.max_total_cost_usd = 0.001
    repo.set_hunger_policy("t1", policy)
    clock = repo.get_hunger_clock("t1")
    clock.consumed_by_cost_usd = 0.5
    repo.save_hunger_clock(clock)

    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient(),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.SAFETY_STOP
    assert report.goal_status == "abandoned"
