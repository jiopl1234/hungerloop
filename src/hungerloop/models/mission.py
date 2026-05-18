"""Mission runtime models for HungerLoop v0.6.

:class:`MissionPhase` implements REQ-M1-001.
:class:`MissionFeature` implements REQ-M1-002.
:class:`Mission` implements REQ-M1-003 and reserves mission-level fields used by
REQ-M3-012 and REQ-M5-043.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MissionPhaseStatus = Literal["pending", "in_progress", "validating", "done"]
MissionFeatureStatus = Literal["pending", "in_progress", "done", "blocked"]


class MissionPhase(BaseModel):
    """Mutable mission phase status model (REQ-M1-001)."""

    phase_id: str
    title: str
    description: str
    feature_ids: list[str] = Field(default_factory=list)
    validation_contract_ids: list[str] = Field(default_factory=list)
    status: MissionPhaseStatus = "pending"
    completed_at: datetime | None = None


class MissionFeature(BaseModel):
    """Mutable mission feature status model (REQ-M1-002)."""

    feature_id: str
    hunger_item_id: str
    phase_id: str
    title: str
    description: str
    preconditions: list[str] = Field(default_factory=list)
    expected_behavior: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    fulfills: list[str] = Field(default_factory=list)
    status: MissionFeatureStatus = "pending"
    assigned_worker_ids: list[str] = Field(default_factory=list)


class Mission(BaseModel):
    """Mission container model (REQ-M1-003)."""

    mission_id: str
    task_id: str
    title: str
    description: str
    phases: list[MissionPhase] = Field(default_factory=list)
    features: list[MissionFeature] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    max_parallel_features: int | None = Field(default=None, ge=1)
    services_manifest: dict[str, Any] | None = None
