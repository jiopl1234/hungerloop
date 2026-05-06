"""Skill card models for HungerLoop (PRD §18 / §20).

* :class:`SkillCard` — shipped v0.5a row. Kept unchanged for backwards
  compatibility; v0.5e.1 callers read from ``active_skill_cards``
  (:class:`ActiveSkillCard`) instead.
* :class:`SkillCardCandidate` — v0.5e.1 candidate row (PRD §18.3).
  Auto-generated when the §15.2 trigger rule fires; an operator runs
  ``skill approve`` to promote into an :class:`ActiveSkillCard`.
* :class:`ActiveSkillCard` — v0.5e.1 promoted row (PRD §18 / FR-3).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SkillCandidateState = Literal["candidate", "active", "rejected", "deprecated"]


class SkillCard(BaseModel):
    """Reusable procedural record of a completed task (v0.5a).

    The shipped table; v0.5e.1 introduces ActiveSkillCard alongside.
    """

    skill_id: str
    task_id: str
    name: str
    task_description: str = ""

    trigger_signals: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)

    accepted_check_keys: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    created_at: str = ""  # ISO-8601 UTC; CLI formats, orchestrator fills


class SkillCardCandidate(BaseModel):
    """Auto-generated skill record awaiting operator approval (PRD §18.3)."""

    skill_candidate_id: str
    task_id: str
    source_best_state_id: str
    source_stop_report_id: str | None = None

    name: str
    description: str = ""

    trigger_signals: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    tools_used: list[str] = Field(default_factory=list)
    accepted_check_keys: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    known_failures: list[str] = Field(default_factory=list)
    reuse_notes: list[str] = Field(default_factory=list)

    state: SkillCandidateState = "candidate"
    created_at: datetime


class ActiveSkillCard(BaseModel):
    """Operator-promoted skill (PRD §18 / FR-3).

    Created by ``skill approve`` from a :class:`SkillCardCandidate`,
    or by ``skill import`` from a YAML export. The shipped
    :class:`SkillCard` table is no longer written by the
    orchestrator; v0.5e.1 callers read from active_skill_cards.
    """

    skill_id: str
    source_candidate_id: str

    name: str
    description: str = ""

    trigger_signals: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    tools_used: list[str] = Field(default_factory=list)
    accepted_check_keys: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    known_failures: list[str] = Field(default_factory=list)
    reuse_notes: list[str] = Field(default_factory=list)

    state: Literal["active", "deprecated"] = "active"
    activated_at: datetime
    activated_by: str
