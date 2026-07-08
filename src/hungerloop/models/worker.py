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

from pydantic import BaseModel, Field, field_validator

from hungerloop.models.refactor import RefactorProposalPayload
from hungerloop.models.synthesis import CheckProposal

AgentKind = Literal["execution", "learning", "research", "planner"]
HandoffItemType = Literal[
    "blocker",
    "follow_up",
    "discovered_issue",
    "incomplete_work",
    "critical_context",
    "refactor_proposal",
]


def _clip_text(value: str, max_length: int) -> str:
    """Clip text fields to the maximum schema length."""
    return value[:max_length]


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


class HandoffItem(BaseModel):
    """Structured worker handoff item (REQ-M2-002)."""

    item_type: HandoffItemType
    summary: str = ""
    detail: str = ""
    related_feature_ids: list[str] = Field(default_factory=list)
    related_check_keys: list[str] = Field(default_factory=list)
    related_item_ids: list[str] = Field(default_factory=list)
    requires_orchestrator_action: bool = False

    # v0.7: deterministic check proposals attached to discovered test-gap items.
    # Defaults to an empty list; valid CheckProposal payloads are accepted,
    # malformed or disallowed proposals are rejected at model-construction time.
    proposed_checks: list[CheckProposal] = Field(default_factory=list)

    # v0.7: refactor proposal payload for ``refactor_proposal`` items.
    # Carries the action (open/close), declared regression keys, and rationale.
    # Workers cannot supply deadlines through this payload (VAL-REF-026).
    refactor_proposal_payload: RefactorProposalPayload | None = None

    @field_validator("summary")
    @classmethod
    def _clip_summary(cls, value: str) -> str:
        return _clip_text(value, 200)

    @field_validator("detail")
    @classmethod
    def _clip_detail(cls, value: str) -> str:
        return _clip_text(value, 2000)


class WorkerHandoff(WorkerResult):
    """Field-additive worker handoff model (REQ-M2-003)."""

    handoff_id: str | None = None
    assignment_id: str | None = None
    retry_count: int = Field(default=0, ge=0)
    handoff_items: list[HandoffItem] = Field(default_factory=list)
    what_was_done: list[str] = Field(default_factory=list)
    what_was_left_undone: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    next_worker_hint: str | None = None

    def as_worker_result(self) -> WorkerResult:
        """Return the v0.5f-compatible WorkerResult view (REQ-M2-004)."""
        return WorkerResult(
            **{
                field_name: getattr(self, field_name)
                for field_name in WorkerResult.model_fields
            }
        )
