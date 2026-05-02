"""Blackboard state models for HungerLoop v0.4.1.

:class:`BestState` is the committed state for a task (the "best" snapshot that
has passed validation). :class:`CandidateState` is the proposed state from a
single loop iteration that may or may not be promoted to ``BestState``. The
``workspace_ref`` fields tie each state to an on-disk workspace managed by
:class:`~hungerloop.services.workspace_manager.WorkspaceManager`.

:class:`Artifact` is the v0.5a descriptor for a tool-produced artifact
(file, patch, note). Returned by ``repo.get_artifacts_by_ids`` and used by
the ``artifact_type_exists`` acceptance check.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Artifact(BaseModel):
    """Descriptor of an agent-produced artifact (new in v0.5a)."""

    artifact_id: str
    task_id: str
    loop_id: int
    artifact_type: str
    path: str | None = None
    summary: str = ""


class BestState(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    state_id: str
    summary: str

    score: float = 0.0

    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    validation_id: str | None = None
    updated_at_loop: int = 0

    accepted_check_keys: list[str] = Field(default_factory=list)
    workspace_ref: str = "best"


class CandidateState(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    loop_id: int
    summary: str

    artifact_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    proposed_score: float = 0.0

    workspace_ref: str
