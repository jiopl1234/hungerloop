from pathlib import Path

import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.workspace_manager import WorkspaceManager


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")
