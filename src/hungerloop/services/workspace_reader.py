"""Read-only workspace inventory protocol for context construction."""
from __future__ import annotations

from typing import Literal, Protocol


class WorkspaceReader(Protocol):
    """List files inside task workspace trees without mutating them."""

    def list_workspace_files(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[str]: ...
