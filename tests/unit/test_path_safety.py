from pathlib import Path

import pytest

from hungerloop.services.path_safety import resolve_workspace_path


def test_relative_path_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "sub").mkdir()
    (workspace / "sub" / "file.txt").touch()

    result = resolve_workspace_path(workspace, "sub/file.txt")
    assert result == workspace / "sub" / "file.txt"


def test_absolute_path_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(PermissionError, match="Absolute path"):
        resolve_workspace_path(workspace, "/etc/passwd")


def test_traversal_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(PermissionError, match="escapes workspace"):
        resolve_workspace_path(workspace, "../../../etc/passwd")


def test_empty_path_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Empty path"):
        resolve_workspace_path(workspace, "")


def test_whitespace_only_path_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Empty path"):
        resolve_workspace_path(workspace, "   ")


def test_dot_path_resolves_to_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = resolve_workspace_path(workspace, ".")
    assert result == workspace.resolve()
