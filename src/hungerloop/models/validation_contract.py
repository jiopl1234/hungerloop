"""Validation contract models for HungerLoop v0.6.

:class:`ValidationAssertion` implements REQ-M1-004.
:class:`ValidationContract` implements REQ-M1-005.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ValidationAssertionStatus = Literal["pending", "passed", "failed", "blocked"]


class ValidationAssertion(BaseModel):
    """Mutable validation assertion record (REQ-M1-004)."""

    assertion_id: str
    phase_id: str
    title: str
    description: str
    check_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    evidence_requirements: list[str] = Field(default_factory=list)
    status: ValidationAssertionStatus = "pending"
    validated_at_loop: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ValidationContract(BaseModel):
    """Mission validation contract helpers (REQ-M1-005)."""

    mission_id: str
    assertions: list[ValidationAssertion] = Field(default_factory=list)

    def assertions_by_phase(self, phase_id: str) -> list[ValidationAssertion]:
        return [
            assertion
            for assertion in self.assertions
            if assertion.phase_id == phase_id
        ]

    def pending_assertions(self) -> list[ValidationAssertion]:
        return [
            assertion
            for assertion in self.assertions
            if assertion.status == "pending"
        ]

    def phase_is_validated(self, phase_id: str) -> bool:
        phase_assertions = self.assertions_by_phase(phase_id)
        return bool(phase_assertions) and all(
            assertion.status == "passed" for assertion in phase_assertions
        )
