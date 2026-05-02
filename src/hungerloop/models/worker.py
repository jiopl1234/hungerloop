"""Worker models for HungerLoop v0.5a.

:class:`AgentSpec` describes a registered worker (v0.5a hardcodes
``execution_worker_v1``; PRD §6 / §28.8 / M2).

:class:`WorkerResult` is the output from one agent's work in a loop iteration.
Extended in v0.5a with ``error_type``, ``requires_human``, ``retryable``,
``llm_call_ids``, ``tool_call_ids`` so the WorkerRuntime can map worker
outcomes onto Orchestrator stop reasons (PRD §3.2 / §28.6).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentKind = Literal["execution", "learning", "research", "planner"]


class AgentSpec(BaseModel):
    """Specification for an agent."""

    agent_id: str
    name: str
    kind: AgentKind = "execution"
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

    llm_call_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)

    error: str | None = None
    error_type: str | None = None
    requires_human: bool = False
    retryable: bool = False
