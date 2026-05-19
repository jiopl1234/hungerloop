from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from hungerloop.models import HandoffItem, HandoffItemType, WorkerHandoff
from hungerloop.models.worker import HandoffItem as HandoffItemFromModule
from hungerloop.models.worker import WorkerHandoff as WorkerHandoffFromModule
from hungerloop.models.worker import WorkerResult


def _make_handoff_item() -> HandoffItem:
    return HandoffItem(
        item_type="follow_up",
        summary="Need a follow-up",
        detail="Investigate the remaining edge case before closing the feature.",
        related_feature_ids=["m2-handoff-models"],
        related_check_keys=["VAL-M2-001"],
        related_item_ids=["H-1"],
        requires_orchestrator_action=False,
    )


def _make_worker_handoff() -> WorkerHandoff:
    return WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=7,
        summary="Implemented the initial model slice.",
        artifact_ids=["artifact-1"],
        evidence_ids=["evidence-1"],
        claim_ids=["claim-1"],
        llm_call_ids=["llm-1"],
        tool_call_ids=["tool-1"],
        error="",
        error_type="",
        requires_human=True,
        retryable=True,
        handoff_items=[_make_handoff_item()],
        what_was_done=["Added WorkerHandoff", "Added HandoffItem"],
        what_was_left_undone=["Wire repository persistence"],
        verification_commands=["pytest -q tests/unit/test_worker_handoff.py"],
        next_worker_hint="Implement repository save/list support next.",
    )


def test_worker_handoff_models_are_exported_and_documented() -> None:
    assert WorkerHandoff is WorkerHandoffFromModule
    assert HandoffItem is HandoffItemFromModule
    assert get_args(HandoffItemType) == (
        "blocker",
        "follow_up",
        "discovered_issue",
        "incomplete_work",
        "critical_context",
    )
    assert "REQ-M2-002" in (HandoffItem.__doc__ or "")
    assert "REQ-M2-003" in (WorkerHandoff.__doc__ or "")


def test_roundtrip_with_new_fields() -> None:
    handoff = _make_worker_handoff()

    round_tripped = WorkerHandoff.model_validate_json(handoff.model_dump_json())

    assert round_tripped == handoff


def test_v0_5f_json_compatible() -> None:
    legacy_result = WorkerResult(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=8,
        summary="Legacy worker result",
        artifact_ids=["artifact-1"],
        evidence_ids=["evidence-1"],
        claim_ids=["claim-1"],
        llm_call_ids=["llm-1"],
        tool_call_ids=["tool-1"],
        error="legacy_error",
        error_type="legacy_type",
        requires_human=True,
        retryable=True,
    )

    parsed = WorkerHandoff.model_validate_json(legacy_result.model_dump_json())

    assert parsed.as_worker_result() == legacy_result
    assert parsed.handoff_items == []
    assert parsed.what_was_done == []
    assert parsed.what_was_left_undone == []
    assert parsed.verification_commands == []
    assert parsed.next_worker_hint is None


def test_as_worker_result_preserves_v0_5f_fields() -> None:
    handoff = _make_worker_handoff()

    worker_result = handoff.as_worker_result()
    expected = {
        field_name: getattr(handoff, field_name)
        for field_name in WorkerResult.model_fields
    }

    assert worker_result.model_dump() == expected


def test_handoff_item_clipping() -> None:
    item = HandoffItem(
        item_type="critical_context",
        summary="s" * 500,
        detail="d" * 5000,
    )

    assert len(item.summary) == 200
    assert item.summary == "s" * 200
    assert len(item.detail) == 2000
    assert item.detail == "d" * 2000


def test_handoff_item_rejects_unknown_item_type() -> None:
    with pytest.raises(ValidationError):
        HandoffItem(
            item_type="random_thing",  # type: ignore[arg-type]
            summary="oops",
            detail="oops",
        )
