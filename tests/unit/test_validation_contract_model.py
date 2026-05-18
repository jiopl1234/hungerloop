from __future__ import annotations

import pytest
from pydantic import ValidationError

from hungerloop.models import ValidationAssertion, ValidationContract
from hungerloop.models.validation_contract import (
    ValidationAssertion as ValidationAssertionFromModule,
)
from hungerloop.models.validation_contract import (
    ValidationContract as ValidationContractFromModule,
)


def _make_assertion(
    assertion_id: str,
    phase_id: str,
    *,
    status: str = "pending",
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=assertion_id,
        phase_id=phase_id,
        title=f"Assertion {assertion_id}",
        description="Check behavior",
        check_type="python",
        params={"command": "echo ok"},
        evidence_requirements=["stdout"],
        status=status,
    )


def test_validation_models_are_exported_and_defaults_are_pending() -> None:
    assertion = _make_assertion("VAL-1", "phase-1")

    assert ValidationAssertion is ValidationAssertionFromModule
    assert ValidationContract is ValidationContractFromModule
    assert assertion.status == "pending"
    assert assertion.validated_at_loop is None
    assert assertion.evidence_ids == []
    assert "REQ-M1-004" in (ValidationAssertion.__doc__ or "")


def test_validation_assertion_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _make_assertion("VAL-1", "phase-1", status="mystery")


def test_assertions_by_phase_preserves_declaration_order() -> None:
    first = _make_assertion("VAL-1", "phase-1")
    second = _make_assertion("VAL-2", "phase-2")
    third = _make_assertion("VAL-3", "phase-1", status="passed")
    contract = ValidationContract(
        mission_id="mission-1",
        assertions=[first, second, third],
    )

    assert contract.assertions_by_phase("phase-1") == [first, third]
    assert "REQ-M1-005" in (ValidationContract.__doc__ or "")


def test_pending_assertions_returns_only_pending_items() -> None:
    pending_a = _make_assertion("VAL-1", "phase-1")
    passed = _make_assertion("VAL-2", "phase-1", status="passed")
    failed = _make_assertion("VAL-3", "phase-1", status="failed")
    blocked = _make_assertion("VAL-4", "phase-1", status="blocked")
    pending_b = _make_assertion("VAL-5", "phase-2")
    contract = ValidationContract(
        mission_id="mission-1",
        assertions=[pending_a, passed, failed, blocked, pending_b],
    )

    assert contract.pending_assertions() == [pending_a, pending_b]


def test_phase_is_validated_requires_assertions_and_all_passed() -> None:
    pending = _make_assertion("VAL-1", "phase-1")
    passed = _make_assertion("VAL-2", "phase-1", status="passed")
    contract = ValidationContract(
        mission_id="mission-1",
        assertions=[passed, pending],
    )

    assert contract.phase_is_validated("phase-1") is False
    assert contract.phase_is_validated("phase-missing") is False

    pending.status = "passed"

    assert contract.phase_is_validated("phase-1") is True
