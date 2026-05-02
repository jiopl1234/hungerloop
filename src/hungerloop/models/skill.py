"""Skill card model for HungerLoop v0.5a.

:class:`SkillCard` summarises a successful task for reuse. Generated only
when ``stop_reason == DONE`` AND ``len(best.accepted_check_keys) >= 2``
(PRD §20.2 / §28 M8).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SkillCard(BaseModel):
    """Reusable procedural record of a completed task."""

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
