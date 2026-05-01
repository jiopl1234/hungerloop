"""Context models for HungerLoop v0.4.1.

:class:`ContextPack` is the agent's execution context for a single loop iteration.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ContextPack(BaseModel):
    """Agent execution context for one loop iteration."""

    task_id: str
    loop_id: int
    agent_id: str
    mission: str
    phase: str

    target_hunger_item_ids: list[str]
    acceptance_criteria: list[str] = Field(default_factory=list)

    best_state_summary: str | None = None
    best_workspace_ref: str = "best"
    candidate_workspace_ref: str

    relevant_claim_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    failure_patterns_to_avoid: list[str] = Field(default_factory=list)

    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)

    budget: dict[str, object] = Field(default_factory=dict)
    required_output_schema: str = ""
