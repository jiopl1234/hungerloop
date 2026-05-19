"""Handoff processing models for HungerLoop v0.6."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

DiscoveredFactKind = Literal["mission_feature", "blocker_note", "test_gap"]


def _clip_text(value: str, max_length: int) -> str:
    """Clip text fields to the maximum schema length."""
    return value[:max_length]


class DiscoveredFact(BaseModel):
    """Compiled discovered fact model (REQ-M2-040)."""

    kind: DiscoveredFactKind
    title: str
    description: str
    source_handoff_id: str
    related_feature_ids: list[str] = Field(default_factory=list)


class HandoffProcessingResult(BaseModel):
    """Structured handoff processing result (REQ-M2-020)."""

    prior_handoff_summary: str = ""
    discovered_issues: list[DiscoveredFact] = Field(default_factory=list)
    blocked_item_ids: list[str] = Field(default_factory=list)
    injected_hunger_item_ids: list[str] = Field(default_factory=list)

    @field_validator("prior_handoff_summary")
    @classmethod
    def _clip_prior_handoff_summary(cls, value: str) -> str:
        return _clip_text(value, 800)
