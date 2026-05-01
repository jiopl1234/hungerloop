from pathlib import Path

import pytest

from hungerloop.services.workspace_manager import WorkspaceManager


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


def test_create_candidate_from_empty_best(ws: WorkspaceManager) -> None:
    candidate = ws.create_candidate_workspace("task_001", loop_id=1)
    assert candidate.exists()
    assert candidate.is_dir()


def test_candidate_does_not_affect_best(ws: WorkspaceManager) -> None:
    ws.ensure_task_workspace("task_001")
    best = ws.best_files_dir("task_001")
    (best / "stable.py").write_text("original")

    candidate = ws.create_candidate_workspace("task_001", loop_id=1)
    (candidate / "stable.py").write_text("modified by candidate")
    (candidate / "new_file.py").write_text("new")

    assert (best / "stable.py").read_text() == "original"
    assert not (best / "new_file.py").exists()


def test_promote_updates_best(ws: WorkspaceManager) -> None:
    ws.ensure_task_workspace("task_001")
    best = ws.best_files_dir("task_001")
    (best / "app.py").write_text("v1")

    candidate = ws.create_candidate_workspace("task_001", loop_id=1)
    (candidate / "app.py").write_text("v2")
    (candidate / "new.py").write_text("new")

    ws.promote_candidate_to_best("task_001", loop_id=1)

    assert (best / "app.py").read_text() == "v2"
    assert (best / "new.py").read_text() == "new"


def test_reject_preserves_best(ws: WorkspaceManager) -> None:
    ws.ensure_task_workspace("task_001")
    best = ws.best_files_dir("task_001")
    (best / "app.py").write_text("v1")

    candidate = ws.create_candidate_workspace("task_001", loop_id=2)
    (candidate / "app.py").write_text("broken")

    ws.reject_candidate("task_001", loop_id=2)

    assert (best / "app.py").read_text() == "v1"
    rejected = ws.rejected_files_dir("task_001", loop_id=2)
    assert (rejected / "app.py").read_text() == "broken"


def test_reject_moves_candidate_to_rejected(ws: WorkspaceManager) -> None:
    candidate = ws.create_candidate_workspace("task_001", loop_id=3)
    (candidate / "test.txt").write_text("data")

    ws.reject_candidate("task_001", loop_id=3)

    assert not candidate.exists()
    rejected = ws.rejected_files_dir("task_001", loop_id=3)
    assert (rejected / "test.txt").read_text() == "data"


def test_promote_nonexistent_candidate_raises(ws: WorkspaceManager) -> None:
    ws.ensure_task_workspace("task_001")
    with pytest.raises(FileNotFoundError):
        ws.promote_candidate_to_best("task_001", loop_id=99)


def test_manifest_written_on_create(ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("task_001", loop_id=1)
    manifest_path = ws.task_root("task_001") / "candidates" / "loop_001" / "manifest.json"
    assert manifest_path.exists()
