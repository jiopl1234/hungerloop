from pathlib import Path
from typing import Literal

import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.workspace_manager import WorkspaceManager
from hungerloop.services.workspace_reader import WorkspaceReader


class FakeWorkspaceReader:
    def __init__(self, files: dict[tuple[str, str, int | None], list[str]] | None = None) -> None:
        self.files = files or {}

    def list_workspace_files(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[str]:
        return list(self.files.get((task_id, ref, loop_id), []))


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def fake_workspace_reader() -> WorkspaceReader:
    return FakeWorkspaceReader()


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")
