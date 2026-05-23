"""LoopTrace v2 / StopReport v2 additive-field contract (PRD §8 + §9).

Pins:

* New fields default to their empty value so c0-01 / v0.5b-shipped
  rows deserialise without migration.
* STOP_REASON_HINTS is keyed by ``StopReason.value`` (not enum name)
  so callers can do ``hints[report.stop_reason.value]`` directly.
* The shipped ``recommendation`` string field coexists with the new
  ``recommended_next_actions`` list — both ship to operators.
"""
from __future__ import annotations

import pytest

from hungerloop.models.enums import StopReason
from hungerloop.models.tracing import (
    STOP_REASON_HINTS,
    LoopTrace,
    StopReport,
)

# ---------------------------------------------------------------------------
# LoopTrace v2 — additive fields default to empty
# ---------------------------------------------------------------------------


def test_loop_trace_v2_defaults_empty() -> None:
    """A trace constructed with only the v0.5a-required fields must
    populate every v2 field with its empty default."""
    trace = LoopTrace(
        task_id="t1",
        loop_id=1,
        phase="explore",
        active_hunger=80.0,
        drive_budget=80.0,
        work_pressure=10.0,
        committed=False,
    )
    assert trace.currently_passed_check_keys == []
    assert trace.satisfied_hunger_item_ids == []
    assert trace.unsatisfied_hunger_item_ids == []
    assert trace.best_state_id_before_loop is None
    assert trace.best_state_id_after_loop is None
    assert trace.verdict is None
    assert trace.model_errors == []
    assert trace.tool_errors == []
    assert trace.worker_errors == []
    assert trace.mission_snapshot is None
    assert trace.assignment_traces == []
    assert trace.validation_pipeline_trace is None


def test_loop_trace_v2_round_trips_through_pydantic_dump() -> None:
    """Full population round-trip: constructed → model_dump → revalidate."""
    trace = LoopTrace(
        task_id="t1",
        loop_id=2,
        phase="exploit",
        active_hunger=50.0,
        drive_budget=50.0,
        work_pressure=0.0,
        committed=True,
        currently_passed_check_keys=["H-001:0", "H-001:1"],
        satisfied_hunger_item_ids=["H-001"],
        unsatisfied_hunger_item_ids=["H-002"],
        best_state_id_before_loop="STATE-1",
        best_state_id_after_loop="STATE-2",
        verdict="pass",
        model_errors=["openai timeout"],
        tool_errors=["bash exit 127"],
        worker_errors=["RuntimeError: boom"],
        mission_snapshot={
            "mission_id": "mission-1",
            "phases": [{"phase_id": "phase-1", "status": "in_progress"}],
            "features": [{"feature_id": "feature-1", "status": "pending"}],
        },
        assignment_traces=[
            {
                "assignment_id": "ASGN-t1-2-0",
                "agent_id": "execution_worker_v1",
                "handoff_id": "WH-t1-2-ASGN-t1-2-0",
                "target_hunger_item_ids": ["H-001"],
                "target_feature_ids": ["feature-1"],
                "summary": "done",
                "evidence_ids": ["EV-1"],
                "error_type": None,
            }
        ],
        validation_pipeline_trace={
            "pipeline_verdict": "pass",
            "stages_run": ["deterministic", "scrutiny", "user_testing"],
            "deterministic_report_id": "VAL-deterministic",
            "scrutiny_report_id": "VAL-scrutiny",
            "user_testing_report_id": "VAL-user-testing",
        },
    )
    blob = trace.model_dump()
    revived = LoopTrace.model_validate(blob)
    assert revived == trace


def test_loop_trace_v0_5a_shape_still_loads() -> None:
    """A dict matching the v0.5a-era shipped shape (no v2 fields) must
    deserialise without error — proves backwards compat for any rows
    persisted before v0.5d.0 lands."""
    legacy_blob: dict[str, object] = {
        "task_id": "t1",
        "loop_id": 1,
        "phase": "explore",
        "active_hunger": 80.0,
        "drive_budget": 80.0,
        "work_pressure": 10.0,
        "committed": False,
        "selected_hunger_item_ids": [],
        "worker_ids": [],
        "newly_passed_check_keys": [],
        "regressed_check_keys": [],
        "blocked_items_added": [],
        "tokens_consumed_this_loop": 0,
        "cost_this_loop_usd": 0.0,
        "llm_calls": 0,
        "tool_calls": 0,
        "delta_summary": "",
        "blocked_item_ids": [],
        "next_action": "continue",
    }
    trace = LoopTrace.model_validate(legacy_blob)
    # Defaults applied for all v2 fields.
    assert trace.currently_passed_check_keys == []
    assert trace.best_state_id_after_loop is None
    assert trace.worker_errors == []
    assert trace.mission_snapshot is None
    assert trace.assignment_traces == []
    assert trace.validation_pipeline_trace is None


# ---------------------------------------------------------------------------
# StopReport v2 — additive fields + STOP_REASON_HINTS
# ---------------------------------------------------------------------------


def test_stop_report_v2_defaults_empty() -> None:
    """v0.5d.0 adds list / scalar fields with safe defaults so the
    c0-01 ``accepted_check_keys_count`` int continues to work and new
    consumers can read the list form too."""
    report = StopReport(
        task_id="t1",
        stop_reason=StopReason.DONE,
        goal_status="completed",
    )
    assert report.accepted_check_keys == []
    assert report.llm_calls == 0
    assert report.tool_calls == 0
    assert report.last_validation_report_id is None
    assert report.last_loop_id is None
    assert report.recommended_next_actions == []
    assert report.resume_hint is None
    # Shipped fields unchanged.
    assert report.accepted_check_keys_count == 0
    assert report.recommendation == ""


def test_stop_report_v2_full_round_trip() -> None:
    report = StopReport(
        task_id="t1",
        stop_reason=StopReason.DONE,
        goal_status="completed",
        accepted_check_keys=["H-001:0", "H-002:0"],
        accepted_check_keys_count=2,
        llm_calls=5,
        tool_calls=12,
        last_validation_report_id="VAL-99",
        last_loop_id=42,
        recommended_next_actions=["run --reset to start a new task"],
        resume_hint="explicit-override-hint",
    )
    blob = report.model_dump()
    revived = StopReport.model_validate(blob)
    assert revived == report


@pytest.mark.parametrize(
    "stop_reason",
    [
        StopReason.DONE,
        StopReason.HUNGER_EXPIRED,
        StopReason.BLOCKED,
        StopReason.HUMAN_REQUIRED,
        StopReason.HUMAN_PAUSED,
        StopReason.SAFETY_STOP,
    ],
)
def test_stop_reason_hint_exists_for_every_shipped_reason(
    stop_reason: StopReason,
) -> None:
    """STOP_REASON_HINTS is keyed by ``.value``; every shipped
    StopReason must have a matching hint so the orchestrator's
    ``resume_hint`` auto-population (FR-11) never produces a None.
    """
    assert stop_reason.value in STOP_REASON_HINTS
    assert STOP_REASON_HINTS[stop_reason.value]


def test_stop_reason_hints_lookup_by_value_not_name() -> None:
    """Pin the §9.3 contract — keys are .value strings, not enum names.
    A regression here means callers writing ``hints[reason.name]`` would
    silently start producing KeyError."""
    assert "done" in STOP_REASON_HINTS
    assert "DONE" not in STOP_REASON_HINTS