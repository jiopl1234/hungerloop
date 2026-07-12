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


def test_create_candidate_can_seed_from_project_source(
    ws: WorkspaceManager, tmp_path: Path
) -> None:
    source = tmp_path / "project"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    (source / "tests" / "test_app.py").write_text("def test_ok(): pass", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: main\n", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv" / "ignored.py").write_text("", encoding="utf-8")

    candidate = ws.create_candidate_workspace(
        "task_001",
        loop_id=1,
        seed_source_dir=source,
    )

    assert (candidate / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')"
    assert (candidate / "tests" / "test_app.py").exists()
    assert (candidate / "pyproject.toml").exists()
    assert not (candidate / ".git").exists()
    assert not (candidate / ".venv").exists()


def test_best_workspace_overlays_seeded_project_source(
    ws: WorkspaceManager, tmp_path: Path
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "app.py").write_text("source", encoding="utf-8")
    ws.ensure_task_workspace("task_001")
    (ws.best_files_dir("task_001") / "app.py").write_text("best", encoding="utf-8")

    candidate = ws.create_candidate_workspace(
        "task_001",
        loop_id=1,
        seed_source_dir=source,
    )

    assert (candidate / "app.py").read_text(encoding="utf-8") == "best"


def test_seed_source_skips_hungerloop_runtime_tree(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "app.py").write_text("source", encoding="utf-8")
    (source / "hungerloop.sqlite").write_text("db", encoding="utf-8")
    wm = WorkspaceManager(source)

    candidate = wm.create_candidate_workspace(
        "task_001",
        loop_id=1,
        seed_source_dir=source,
    )

    assert (candidate / "app.py").exists()
    assert not (candidate / "tasks").exists()
    assert not (candidate / "hungerloop.sqlite").exists()


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


def test_rejected_candidate_can_seed_next_candidate_without_touching_best(
    ws: WorkspaceManager,
) -> None:
    ws.ensure_task_workspace("task_001")
    best = ws.best_files_dir("task_001")
    (best / "app.py").write_text("v1", encoding="utf-8")
    rejected_candidate = ws.create_candidate_workspace("task_001", loop_id=1)
    (rejected_candidate / "app.py").write_text("partial", encoding="utf-8")
    ws.reject_candidate("task_001", loop_id=1)

    continued = ws.create_candidate_workspace("task_001", loop_id=2)
    assert ws.continue_candidate_from_rejected("task_001", loop_id=2) is True
    assert continued.joinpath("app.py").read_text(encoding="utf-8") == "partial"
    assert best.joinpath("app.py").read_text(encoding="utf-8") == "v1"
    assert ws.rejected_files_dir("task_001", 1).joinpath("app.py").read_text(
        encoding="utf-8"
    ) == "partial"


def test_identical_rejected_candidate_is_not_carried_forward(
    ws: WorkspaceManager,
) -> None:
    ws.ensure_task_workspace("task_001")
    ws.create_candidate_workspace("task_001", loop_id=1)
    ws.reject_candidate("task_001", loop_id=1)
    ws.create_candidate_workspace("task_001", loop_id=2)

    assert ws.continue_candidate_from_rejected("task_001", loop_id=2) is False


def test_runtime_cache_only_rejected_candidate_is_not_carried_forward(
    ws: WorkspaceManager,
) -> None:
    ws.ensure_task_workspace("task_001")
    candidate = ws.create_candidate_workspace("task_001", loop_id=1)
    cache = candidate / "__pycache__"
    cache.mkdir()
    (cache / "app.pyc").write_bytes(b"cache")
    ws.reject_candidate("task_001", loop_id=1)
    ws.create_candidate_workspace("task_001", loop_id=2)

    assert ws.continue_candidate_from_rejected("task_001", loop_id=2) is False


def test_promote_nonexistent_candidate_raises(ws: WorkspaceManager) -> None:
    ws.ensure_task_workspace("task_001")
    with pytest.raises(FileNotFoundError):
        ws.promote_candidate_to_best("task_001", loop_id=99)


def test_manifest_written_on_create(ws: WorkspaceManager) -> None:
    ws.create_candidate_workspace("task_001", loop_id=1)
    manifest_path = ws.task_root("task_001") / "candidates" / "loop_001" / "manifest.json"
    assert manifest_path.exists()
