"""Orchestrator §7.5 event ordering tests (PRD §7.5).

Pins the canonical per-loop event sequence the orchestrator emits in
v0.5d.0:

    LOOP_STARTED → LOOP_PLANNED → (WORKER_STARTED → tool/model →
    WORKER_FINISHED|FAILED)+ → CANDIDATE_CREATED → VALIDATION_STARTED →
    (CHECK_PASSED|FAILED|REGRESSED)+ → VALIDATION_FINISHED →
    CANDIDATE_COMMITTED|REJECTED → LOOP_COMMITTED|LOOP_REJECTED.

Plus the terminal `STOP_REPORT_CREATED` whenever a StopReport is built
and the synthetic-worker-exception ERROR persistence path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from tests.integration.conftest import make_seed_report_task, workspace


def _event_types_for(repo: InMemoryRepository, task_id: str) -> list[str]:
    return [str(e["event_type"]) for e in repo.list_events(task_id)]


# ---------------------------------------------------------------------------
# Happy path — every shipped event in the right order
# ---------------------------------------------------------------------------


async def test_happy_path_emits_section_7_5_sequence(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")
    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.DONE

    types = _event_types_for(repo, "t1")

    # Every PRD §7.5 lifecycle event fired at least once across the run.
    must_appear = {
        "loop_started",
        "loop_planned",
        "worker_started",
        "worker_finished",
        "worker.handoff_emitted",
        "worker.handoff_received",
        "candidate_created",
        "validation_started",
        "validation_finished",
        "candidate_committed",
        "loop_committed",
        "stop_report_created",
    }
    missing = must_appear - set(types)
    assert not missing, (
        f"Missing event types: {missing} (got: {sorted(set(types))})"
    )


async def test_first_loop_emits_canonical_ordering(tmp_path: Path) -> None:
    """Within a single loop, the §7.5 ordering is strict.

    Pins the *relative* ordering: every later event's first occurrence
    in the loop must come after every earlier event's first occurrence.
    """
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")
    await orchestrator.run("t1")

    # Filter to loop_id=1 events only.
    loop1 = [
        str(e["event_type"])
        for e in repo.list_events("t1", since_loop=1, until_loop=1)
    ]

    def first_index(name: str) -> int:
        return loop1.index(name) if name in loop1 else -1

    # Canonical ordering — each "earlier" event appears before "later".
    canonical = [
        "loop_started",
        "loop_planned",
        "worker_started",
        "worker_finished",
        "worker.handoff_emitted",
        "worker.handoff_received",
        "candidate_created",
        "validation_started",
        "validation_finished",
        "candidate_committed",
        "loop_committed",
    ]
    indices = [first_index(name) for name in canonical]
    # All required events fired this loop.
    assert all(i >= 0 for i in indices), (
        f"Missing one of {canonical} in loop 1: {loop1}"
    )
    # Strictly increasing first-occurrence indices.
    assert indices == sorted(indices), (
        f"Out-of-order events in loop 1: {list(zip(canonical, indices))}"
    )


async def test_check_events_appear_between_validation_started_and_finished(
    tmp_path: Path,
) -> None:
    repo = InMemoryRepository()
    make_seed_report_task()(repo)
    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")
    await orchestrator.run("t1")

    loop1 = [
        str(e["event_type"])
        for e in repo.list_events("t1", since_loop=1, until_loop=1)
    ]
    started_idx = loop1.index("validation_started")
    finished_idx = loop1.index("validation_finished")
    check_indices = [
        i
        for i, name in enumerate(loop1)
        if name in {"check_passed", "check_failed", "check_regressed"}
    ]
    assert check_indices, (
        "expected at least one check_* event between validation_started "
        "and validation_finished"
    )
    assert all(started_idx < i < finished_idx for i in check_indices)


# ---------------------------------------------------------------------------
# Synthetic worker exception — trace + StopReport persisted
# ---------------------------------------------------------------------------


async def test_synthetic_exception_persists_error_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker raise mid-loop must NOT lose the loop trace. The
    orchestrator's outer try/except in step() persists a LoopTrace
    with stop_reason=ERROR and emits ``error`` + ``stop_report_created``
    events so ``hungerloop trace export`` shows the failure cause.
    """
    repo = InMemoryRepository()
    make_seed_report_task()(repo)

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic worker explosion")

    # Replace the inner runner so the orchestrator's outer try/except sees it.
    monkeypatch.setattr(orchestrator, "_run_assignments", _boom)

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.ERROR
    assert "RuntimeError" in report.recommendation

    types = _event_types_for(repo, "t1")
    assert "error" in types
    assert "stop_report_created" in types

    # An ERROR-marked LoopTrace exists.
    error_traces = [
        t for t in repo.list_loop_traces("t1") if t.stop_reason is StopReason.ERROR
    ]
    assert error_traces, "expected at least one ERROR LoopTrace persisted"
    assert error_traces[0].worker_errors
    assert "RuntimeError" in error_traces[0].worker_errors[0]

    # Post-review I1: the ERROR LoopTrace's loop_id matches the loop the
    # §7.5 events fired under. A drift here means events for the failed
    # loop and the trace describing that failure live under different
    # loop_ids — `repair-state --check D13` would falsely flag the
    # events as orphans.
    error_loop_id = error_traces[0].loop_id
    loop_events_for_error_loop = [
        e for e in repo.list_events("t1") if e.get("loop_id") == error_loop_id
    ]
    event_types_for_error_loop = {
        str(e["event_type"]) for e in loop_events_for_error_loop
    }
    assert "loop_started" in event_types_for_error_loop, (
        f"events for loop {error_loop_id} should include loop_started; "
        f"got {sorted(event_types_for_error_loop)}"
    )
