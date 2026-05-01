"""Path safety utilities for HungerLoop v0.4.1.

Implements invariant I-7 (path safety): prevents directory traversal attacks and
absolute-path escapes when resolving user-supplied paths within a workspace root.
Used by :class:`~hungerloop.services.workspace_manager.WorkspaceManager` and
:class:`~hungerloop.services.sandbox_runner.SandboxRunner`.
"""
from pathlib import Path


def resolve_workspace_path(workspace_root: Path, user_path: str) -> Path:
    """Resolve a user-supplied path within a workspace root, rejecting escapes.

    Args:
        workspace_root: The workspace root directory (must exist).
        user_path: A relative path string supplied by the user or agent.

    Returns:
        The resolved absolute path, guaranteed to be within ``workspace_root``.

    Raises:
        ValueError: If ``user_path`` is empty or whitespace-only.
        PermissionError: If ``user_path`` is absolute or escapes the workspace.

    Examples:
        Resolving a relative path inside the workspace succeeds::

            resolve_workspace_path(Path("/workspace"), "sub/file.txt")
            # returns Path('/workspace/sub/file.txt')

        Traversal attempts are rejected::

            resolve_workspace_path(Path("/workspace"), "../etc/passwd")
            # raises PermissionError: Path escapes workspace: ../etc/passwd
    """
    root = workspace_root.resolve()

    if not user_path.strip():
        raise ValueError("Empty or whitespace-only path is not allowed.")

    raw = Path(user_path)
    if raw.is_absolute():
        raise PermissionError(f"Absolute path is not allowed: {user_path}")

    resolved = (root / raw).resolve()

    if not resolved.is_relative_to(root):
        raise PermissionError(f"Path escapes workspace: {user_path}")

    return resolved
