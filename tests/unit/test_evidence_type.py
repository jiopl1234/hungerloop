"""Unit tests for EvidenceType enum (PRD §3.3, v0.5a)."""
from __future__ import annotations

import pytest

from hungerloop.models.enums import EvidenceType


def test_evidence_type_values_match_repository_taxonomy() -> None:
    """The enum values are the canonical strings written to the evidence table."""
    assert EvidenceType.SANDBOX_RUN.value == "sandbox_run"
    assert EvidenceType.MODEL_CALL.value == "model_call"
    assert EvidenceType.MODEL_ERROR.value == "model_error"
    assert EvidenceType.VALIDATION_CHECK.value == "validation_check"
    assert EvidenceType.TOOL_CALL.value == "tool_call"
    assert EvidenceType.HUMAN_INPUT.value == "human_input"


def test_evidence_type_is_str_enum() -> None:
    """EvidenceType is a str-Enum so values can flow through SQLite TEXT columns."""
    assert isinstance(EvidenceType.SANDBOX_RUN, str)
    assert EvidenceType.SANDBOX_RUN == "sandbox_run"


def test_evidence_type_membership_is_complete() -> None:
    """Lock the v0.5a member set so additions are deliberate (PRD §17.4)."""
    expected = {
        "sandbox_run",
        "model_call",
        "model_error",
        "validation_check",
        "tool_call",
        "human_input",
    }
    assert {e.value for e in EvidenceType} == expected


@pytest.mark.parametrize("value", ["unknown", "shell_output", ""])
def test_evidence_type_rejects_unknown_value(value: str) -> None:
    with pytest.raises(ValueError):
        EvidenceType(value)
