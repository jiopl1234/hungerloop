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

    with pytest.raises(ValueError, match="Empty or whitespace-only"):
        resolve_workspace_path(workspace, "")


def test_whitespace_only_path_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Empty or whitespace-only"):
        resolve_workspace_path(workspace, "   ")


def test_dot_path_resolves_to_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = resolve_workspace_path(workspace, ".")
    assert result == workspace.resolve()


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """A symlink inside the workspace pointing outside must be rejected.

    This is the critical security test: resolve() follows the symlink to its
    target, and the containment check must catch that the target is outside.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (workspace / "link").symlink_to(outside / "secret.txt")

    with pytest.raises(PermissionError, match="escapes workspace"):
        resolve_workspace_path(workspace, "link")


def test_nonexistent_path_still_resolves(tmp_path: Path) -> None:
    """Paths that don't exist yet are allowed (for "about to write" cases)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = resolve_workspace_path(workspace, "new/file.txt")
    assert result == workspace.resolve() / "new" / "file.txt"
    assert not result.exists()
