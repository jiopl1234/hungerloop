"""Workspace manifest model for HungerLoop v0.4.1.

:class:`WorkspaceManifest` is the on-disk descriptor of a candidate or best
workspace (the copy-on-write filesystem managed by
:class:`~hungerloop.services.workspace_manager.WorkspaceManager`). Serialised to
JSON alongside the workspace tree so the manifest survives across process runs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

WorkspaceStatus = Literal["candidate", "best", "rejected"]


class WorkspaceManifest(BaseModel):
    task_id: str
    loop_id: int | None = None
    workspace_ref: str
    path: str

    source_workspace_ref: str | None = None
    status: WorkspaceStatus = "candidate"

    created_by: str | None = None
    created_at: str | None = None

    file_count: int = 0
    total_bytes: int = 0
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
