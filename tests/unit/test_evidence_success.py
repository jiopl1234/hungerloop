"""Unit tests for ``is_successful_evidence_payload`` (Critical-2 fix).

Drives the ``successful_only`` filter in ``count_evidence_by_type`` which in
turn keeps the default ``evidence_count_min`` hunger check from auto-passing
on failed tool calls.
"""
from __future__ import annotations

from hungerloop.models.enums import EvidenceType
from hungerloop.repository.evidence_success import is_successful_evidence_payload


def test_model_call_is_always_successful() -> None:
    assert is_successful_evidence_payload(EvidenceType.MODEL_CALL, {}) is True


def test_model_error_is_never_successful() -> None:
    assert is_successful_evidence_payload(EvidenceType.MODEL_ERROR, {}) is False


def test_tool_call_success_field_is_authoritative() -> None:
    assert (
        is_successful_evidence_payload(EvidenceType.TOOL_CALL, {"success": True})
        is True
    )
    assert (
        is_successful_evidence_payload(EvidenceType.TOOL_CALL, {"success": False})
        is False
    )


def test_tool_call_missing_success_field_is_not_successful() -> None:
    """A tool call payload that doesn't even claim success counts as failed.

    Defends against the original Critical-2 bug where 'bad_args' tool failures
    still inflated evidence_count_min because the type alone was matched.
    """
    assert is_successful_evidence_payload(EvidenceType.TOOL_CALL, {}) is False


def test_sandbox_run_requires_exit_zero_and_no_timeout() -> None:
    ok = {"exit_code": 0, "timed_out": False}
    bad_exit = {"exit_code": 1, "timed_out": False}
    timed_out = {"exit_code": 0, "timed_out": True}
    assert is_successful_evidence_payload(EvidenceType.SANDBOX_RUN, ok) is True
    assert is_successful_evidence_payload(EvidenceType.SANDBOX_RUN, bad_exit) is False
    assert is_successful_evidence_payload(EvidenceType.SANDBOX_RUN, timed_out) is False


def test_unknown_evidence_type_is_not_successful() -> None:
    """Future-proofing: unrecognised types default to failure, not success.

    Keeps the count conservative so a new evidence type can't accidentally
    inflate evidence_count_min before its success semantics are defined.
    """
    assert is_successful_evidence_payload("validation_check", {}) is False
    assert is_successful_evidence_payload("human_input", {}) is False
    assert is_successful_evidence_payload("brand_new_type", {}) is False


def test_accepts_string_evidence_type_argument() -> None:
    """The helper supports both EvidenceType enum and raw string."""
    assert is_successful_evidence_payload("model_call", {}) is True
    assert is_successful_evidence_payload("tool_call", {"success": True}) is True
