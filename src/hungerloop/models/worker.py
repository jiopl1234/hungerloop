"""Worker models for HungerLoop v0.4.1.

:class:`WorkerResult` is the output from one agent's work in a loop iteration.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AgentSpec(BaseModel):
    """Specification for an agent."""

    agent_id: str
    name: str
    output_schema_name: str = "default"
    allowed_tools: list[str] = Field(default_factory=list)


class WorkerResult(BaseModel):
    """Result from one agent's work."""

    agent_id: str
    task_id: str
    loop_id: int
    summary: str = ""
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
