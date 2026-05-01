"""Validation data models for HungerLoop v0.4.1.

:class:`CheckResult` captures the outcome of a single acceptance check against a
candidate workspace. :class:`ValidationReport` aggregates those results across a
loop and carries the check-level progress signals that encode invariant I-3:
``newly_passed_check_keys`` / ``regressed_check_keys`` / ``has_real_progress``.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from hungerloop.models.enums import ValidationVerdict


class CheckResult(BaseModel):
    hunger_item_id: str
    check_index: int
    check_key: str
    check_type: str

    passed: bool
    previously_passed: bool = False
    newly_passed: bool = False
    regressed: bool = False

    detail: str
    evidence_id: str | None = None
    workspace_ref: str | None = None


class ValidationReport(BaseModel):
    id: str
    task_id: str
    loop_id: int
    candidate_state_id: str
    baseline_state_id: str | None

    verdict: ValidationVerdict

    score_before: float = 0.0
    score_after: float = 0.0
    score_delta: float = 0.0

    attempted_hunger_item_ids: list[str] = Field(default_factory=list)

    check_results: list[CheckResult] = Field(default_factory=list)

    currently_passed_check_keys: list[str] = Field(default_factory=list)
    newly_passed_check_keys: list[str] = Field(default_factory=list)
    regressed_check_keys: list[str] = Field(default_factory=list)

    satisfied_hunger_item_ids: list[str] = Field(default_factory=list)
    unsatisfied_hunger_item_ids: list[str] = Field(default_factory=list)

    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)

    recommended_next_actions: list[str] = Field(default_factory=list)

    has_real_progress: bool = False
