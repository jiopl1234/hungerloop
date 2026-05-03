"""Unit tests for LoopTrace nullable fields (reverse-spec U10, v0.5a).

Empty-plan / SafetyStop / worker-timeout paths emit a LoopTrace without a
candidate_state_id or validation_report_id. Both must be nullable.
"""
from __future__ import annotations

from hungerloop.models.enums import StopReason
from hungerloop.models.tracing import LoopTrace, StopReport


def test_loop_trace_allows_null_candidate_and_validation_ids() -> None:
    """Empty-plan path: no candidate, no validation, but the trace must be writable."""
    trace = LoopTrace(
        task_id="t1",
        loop_id=1,
        phase="explore",
        active_hunger=80.0,
        drive_budget=80.0,
        work_pressure=1.0,
        committed=False,
        delta_summary="empty plan",
    )
    assert trace.candidate_state_id is None
    assert trace.validation_report_id is None
    assert trace.committed is False


def test_loop_trace_round_trips_with_ids() -> None:
    """Normal path: both ids set."""
    trace = LoopTrace(
        task_id="t1",
        loop_id=2,
        phase="exploit",
        active_hunger=50.0,
        drive_budget=60.0,
        work_pressure=0.8,
        candidate_state_id="CAND-t1-2",
        validation_report_id="VAL-t1-2",
        committed=True,
    )
    assert trace.candidate_state_id == "CAND-t1-2"
    assert trace.validation_report_id == "VAL-t1-2"


def test_stop_report_minimal() -> None:
    report = StopReport(
        task_id="t1", stop_reason=StopReason.DONE, goal_status="completed"
    )
    assert report.task_id == "t1"
    assert report.stop_reason == StopReason.DONE
    assert report.goal_status == "completed"
    assert report.summary == ""
