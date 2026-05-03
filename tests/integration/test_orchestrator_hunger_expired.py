"""HUNGER_EXPIRED path with LOOP_COUNT decay (PRD §23.2)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from tests.integration.conftest import make_seed_report_task, workspace


async def test_loop_count_decay_emits_hunger_expired(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    # decay_duration_seconds=2 in LOOP_COUNT mode means budget exhausts after
    # ~2 fruitless loops; the dummy client never produces actions so the file
    # never appears.
    make_seed_report_task(decay_duration=2.0)(repo)
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient(),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.HUNGER_EXPIRED
    # No best state was ever committed → goal_status="abandoned" per §28.6.
    assert report.goal_status == "abandoned"
