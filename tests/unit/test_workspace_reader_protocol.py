"""WorkspaceReader Protocol tests."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import pytest

from hungerloop.services.workspace_manager import WorkspaceManager
from hungerloop.services.workspace_reader import WorkspaceReader


def _read_best(reader: WorkspaceReader, task_id: str) -> list[str]:
    return reader.list_workspace_files(task_id, ref="best")


def test_workspace_manager_conforms_to_workspace_reader(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    reader = cast(WorkspaceReader, manager)

    assert _read_best(reader, "missing-task") == []


def test_workspace_manager_lists_files_sorted_and_filtered(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    root = manager.best_files_dir("t1")
    (root / "b.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    (root / ".pytest_cache" / "v").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "nodeids").write_text("x", encoding="utf-8")

    assert manager.list_workspace_files("t1", ref="best") == ["a.txt", "b.txt"]


def test_candidate_listing_requires_loop_id(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    with pytest.raises(ValueError):
        manager.list_workspace_files("t1", ref=cast(Literal["candidate"], "candidate"))
