"""End-to-end DONE path with DummyModelClient (PRD §22.1 / §23.2)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from tests.integration.conftest import make_seed_report_task, workspace


async def test_demo_task_reaches_done(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo report\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.DONE
    assert report.goal_status == "completed"
    assert report.accepted_check_keys_count == 1
    # MemoryManager runs per-loop; demo should have produced a candidate.
    assert len(repo.list_memory_candidates("t1")) >= 1
