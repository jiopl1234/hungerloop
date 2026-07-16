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


def test_list_workspace_file_stats(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    best = manager.best_files_dir("t1")
    best.mkdir(parents=True)
    (best / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8", newline="\n")
    (best / "img.bin").write_bytes(b"\xff\xfe\x00\x01")
    stats = manager.list_workspace_file_stats("t1", ref="best")
    assert ("a.py", 12, 2) in stats
    assert ("img.bin", 4, -1) in stats


def test_list_workspace_file_stats_bounds_line_count_reads(tmp_path: Path) -> None:
    """Only the first ``_LINE_COUNT_MAX_FILES`` sorted files are line-counted;
    every file beyond the head is reported size-only (line_count == -1),
    keeping prompt-build I/O bounded regardless of tree size."""
    manager = WorkspaceManager(tmp_path)
    best = manager.best_files_dir("t1")
    best.mkdir(parents=True)
    total = manager._LINE_COUNT_MAX_FILES + 5
    # Zero-padded names so sorted order == creation order == index order.
    for i in range(total):
        (best / f"file_{i:03d}.py").write_text("a\nb\n", encoding="utf-8", newline="\n")

    stats = manager.list_workspace_file_stats("t1", ref="best")
    assert len(stats) == total
    # Result is sorted by path.
    assert [name for name, _size, _lines in stats] == sorted(
        name for name, _size, _lines in stats
    )
    # First _LINE_COUNT_MAX_FILES entries carry real line counts (2 lines each).
    head = stats[: manager._LINE_COUNT_MAX_FILES]
    assert all(lines == 2 for _name, _size, lines in head)
    # Everything beyond the head is size-only (-1), but bytes stay accurate.
    tail = stats[manager._LINE_COUNT_MAX_FILES :]
    assert tail  # there are extra files past the head
    assert all(lines == -1 and size == 4 for _name, size, lines in tail)
