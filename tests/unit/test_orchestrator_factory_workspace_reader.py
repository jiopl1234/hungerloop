"""orchestrator_factory WorkspaceReader wiring tests."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.repository.in_memory_repo import InMemoryRepository


def test_factory_injects_workspace_reader(tmp_path: Path) -> None:
    orchestrator = build_orchestrator(
        repo=InMemoryRepository(),
        workspace_root=tmp_path,
    )

    assert (
        orchestrator.context_builder.workspace_reader.list_workspace_files(
            "missing-task",
            ref="best",
        )
        == []
    )
