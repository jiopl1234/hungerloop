"""I-4 invariant: rejected candidates never end up in best/ (PRD §23.2)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from tests.integration.conftest import make_seed_report_task, workspace


async def test_rejected_candidate_leaves_best_empty(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    # Worker writes a *wrong* file: report.md is the acceptance check, but the
    # dummy writes notes.md. The candidate is therefore rejected (no
    # newly_passed_check_keys → not committable per I-3).
    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "notes.md", "content": "off-target\n"},
        }
    ]
    workspace_root = workspace(tmp_path)
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace_root,
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    await orchestrator.run("t1")

    # Best workspace must remain empty: no commit means I-4 holds.
    best_files = orchestrator.workspace_manager.best_files_dir("t1")
    assert best_files.exists()
    assert list(best_files.iterdir()) == []
    assert repo.get_best_state("t1") is None
