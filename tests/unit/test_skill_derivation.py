"""Skill-card deterministic derivation (PRD §18.4 / §18.5).

Per-helper unit tests + the §18.5 determinism golden test.
"""
from __future__ import annotations

from hungerloop.models.blackboard import BestState
from hungerloop.models.events import EventType
from hungerloop.models.tracing import LoopTrace
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.skill_derivation import (
    derive_failures,
    derive_name,
    derive_preconditions,
    derive_steps,
    derive_tools,
    derive_trigger_signals,
)


def _best(
    *,
    summary: str = "wrote report.md",
    accepted_check_keys: list[str] | None = None,
    artifact_ids: list[str] | None = None,
) -> BestState:
    return BestState(
        task_id="t1",
        state_id="best-1",
        summary=summary,
        accepted_check_keys=accepted_check_keys or [],
        artifact_ids=artifact_ids or [],
    )


def _trace(
    *,
    loop_id: int,
    committed: bool,
    delta_summary: str = "",
) -> LoopTrace:
    return LoopTrace(
        task_id="t1",
        loop_id=loop_id,
        phase="exploit",
        active_hunger=10.0,
        drive_budget=10.0,
        work_pressure=0.0,
        committed=committed,
        delta_summary=delta_summary,
    )


# ---------------------------------------------------------------------------
# derive_name
# ---------------------------------------------------------------------------


def test_derive_name_first_non_empty_line_truncated() -> None:
    best = _best(summary="\n\nfirst line\nsecond line\n")
    assert derive_name(best) == "first line"


def test_derive_name_strips_trailing_punctuation() -> None:
    best = _best(summary="successfully shipped the feature.")
    assert derive_name(best) == "successfully shipped the feature"


def test_derive_name_truncates_at_80() -> None:
    long = "x" * 200
    best = _best(summary=long)
    assert len(derive_name(best)) == 80


def test_derive_name_empty_summary_falls_back_to_task_id() -> None:
    best = _best(summary="")
    assert derive_name(best) == "Skill from task t1"


# ---------------------------------------------------------------------------
# derive_steps
# ---------------------------------------------------------------------------


def test_derive_steps_skips_rejected_loops() -> None:
    traces = [
        _trace(loop_id=1, committed=True, delta_summary="step A"),
        _trace(loop_id=2, committed=False, delta_summary="rejected B"),
        _trace(loop_id=3, committed=True, delta_summary="step C"),
    ]
    assert derive_steps(traces) == ["step A", "step C"]


def test_derive_steps_falls_back_to_loop_id_label() -> None:
    traces = [
        _trace(loop_id=4, committed=True, delta_summary=""),
    ]
    out = derive_steps(traces)
    assert len(out) == 1
    assert "Loop 4" in out[0]


# ---------------------------------------------------------------------------
# derive_tools
# ---------------------------------------------------------------------------


def test_derive_tools_dedupes_and_sorts_by_first_seen_loop() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    # Two loops, three tool calls (one duplicate).
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="tool_a",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="tool_b",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=2,
        agent_id="a1",
        tool_name="tool_a",  # duplicate; first-seen at loop 1
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=2,
        agent_id="a1",
        tool_name="tool_c",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    traces = [
        _trace(loop_id=1, committed=True),
        _trace(loop_id=2, committed=True),
    ]
    # Order: tool_a (loop 1), tool_b (loop 1, secondary by name),
    # tool_c (loop 2).
    assert derive_tools(traces, repo) == ["tool_a", "tool_b", "tool_c"]


def test_derive_tools_ignores_uncommitted_loops() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="rejected_tool",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=2,
        agent_id="a1",
        tool_name="committed_tool",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    traces = [
        _trace(loop_id=1, committed=False),
        _trace(loop_id=2, committed=True),
    ]
    assert derive_tools(traces, repo) == ["committed_tool"]


# ---------------------------------------------------------------------------
# derive_trigger_signals + derive_preconditions
# ---------------------------------------------------------------------------


def test_derive_trigger_signals_includes_check_and_artifact_ids() -> None:
    best = _best(
        accepted_check_keys=["H-001:0", "H-002:1"],
        artifact_ids=["art-1", "art-2"],
    )
    out = derive_trigger_signals(best, [])
    assert "check:H-001" in out
    assert "check:H-002" in out
    assert "artifact:art-1" in out
    assert "artifact:art-2" in out
    # Sorted, unique.
    assert out == sorted(set(out))


def test_derive_preconditions_static_set() -> None:
    best = _best(accepted_check_keys=["H-001:0", "H-002:1"])
    out = derive_preconditions(best, [])
    assert out == [
        "workspace clean (no uncommitted candidates)",
        "previously-passed checks: 2",
    ]


# ---------------------------------------------------------------------------
# derive_failures
# ---------------------------------------------------------------------------


def test_derive_failures_collects_tool_and_model_errors_capped() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    repo.append_event(
        EventType.TOOL_CALL_FAILED,
        {"error_type": "tool_failed"},
        task_id="t1",
        loop_id=2,
    )
    repo.append_event(
        EventType.MODEL_CALL_FAILED,
        {"error_type": "model_timeout"},
        task_id="t1",
        loop_id=3,
    )
    rows = derive_failures("t1", repo)
    assert "tool_call_failed: tool_failed (loop 2)" in rows
    assert "model_call_failed: model_timeout (loop 3)" in rows


def test_derive_failures_caps_at_20() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    for i in range(30):
        repo.append_event(
            EventType.TOOL_CALL_FAILED,
            {"error_type": f"err_{i}"},
            task_id="t1",
            loop_id=i,
        )
    rows = derive_failures("t1", repo, cap=20)
    assert len(rows) == 20
    # Newest kept (loop 10..29).
    assert "err_29" in rows[-1]
    assert "err_10" in rows[0]


# ---------------------------------------------------------------------------
# §18.5 determinism golden
# ---------------------------------------------------------------------------


def test_skill_card_derivation_deterministic() -> None:
    """PRD §18.5 — the same fixture produces byte-identical output
    across two runs of the derivers."""
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    best = BestState(
        task_id="t1",
        state_id="best-1",
        summary="Production deploy succeeded for service X",
        accepted_check_keys=["H-001:0", "H-002:0"],
        artifact_ids=["art-1"],
        evidence_ids=["ev-1"],
    )
    repo.save_best_state(best)
    traces = [
        _trace(loop_id=1, committed=True, delta_summary="delta of loop 1"),
        _trace(loop_id=2, committed=False, delta_summary="rejected"),
        _trace(loop_id=3, committed=True, delta_summary="delta of loop 3"),
        _trace(loop_id=4, committed=False, delta_summary="rejected"),
        _trace(loop_id=5, committed=True, delta_summary="delta of loop 5"),
    ]
    for t in traces:
        repo.save_loop_trace(t)
    for loop, tool in [
        (1, "tool_a"),
        (1, "tool_b"),
        (3, "tool_c"),
        (3, "tool_a"),
        (5, "tool_b"),
    ]:
        repo.save_tool_call_as_evidence(
            task_id="t1",
            loop_id=loop,
            agent_id="a1",
            tool_name=tool,
            args_summary="{}",
            result_summary="ok",
            success=True,
            elapsed_ms=1,
        )
    repo.append_event(
        EventType.TOOL_CALL_FAILED,
        {"error_type": "boom"},
        task_id="t1",
        loop_id=2,
    )

    out1 = {
        "name": derive_name(best),
        "steps": derive_steps(traces),
        "tools": derive_tools(traces, repo),
        "trigger_signals": derive_trigger_signals(best, traces),
        "preconditions": derive_preconditions(best, traces),
        "failures": derive_failures("t1", repo),
    }
    out2 = {
        "name": derive_name(best),
        "steps": derive_steps(traces),
        "tools": derive_tools(traces, repo),
        "trigger_signals": derive_trigger_signals(best, traces),
        "preconditions": derive_preconditions(best, traces),
        "failures": derive_failures("t1", repo),
    }
    assert out1 == out2

    # Pin the §18.5 expected values verbatim.
    assert out1["steps"] == [
        "delta of loop 1",
        "delta of loop 3",
        "delta of loop 5",
    ]
    assert out1["tools"] == ["tool_a", "tool_b", "tool_c"]
    assert out1["trigger_signals"] == [
        "artifact:art-1",
        "check:H-001",
        "check:H-002",
    ]
    assert "tool_call_failed: boom (loop 2)" in out1["failures"]
