from __future__ import annotations

import inspect
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import Assignment, BudgetAllocation
from hungerloop.models.worker import AgentSpec, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import SafetyStopError
from hungerloop.services.worker_scheduler import SchedulerResult, WorkerScheduler
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
LOOP_ID = 1
AGENT_ID = "execution_worker_v1"
MISSION_ID = "mission-1"


def _assignment(
    assignment_id: str,
    *,
    depends_on: list[str] | None = None,
    max_retries: int = 0,
    target_feature_ids: list[str] | None = None,
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        agent_id=AGENT_ID,
        mission=assignment_id,
        target_hunger_item_ids=[f"H-{assignment_id}"],
        target_feature_ids=target_feature_ids or [f"F-{assignment_id}"],
        allowed_tools=["write_file"],
        depends_on=depends_on or [],
        max_retries=max_retries,
    )


def _handoff(
    assignment_id: str,
    *,
    error_type: str | None = None,
    retryable: bool = False,
    evidence_ids: list[str] | None = None,
) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id=AGENT_ID,
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary=f"handoff-{assignment_id}",
        evidence_ids=evidence_ids or [],
        error=f"{error_type}-error" if error_type is not None else None,
        error_type=error_type,
        retryable=retryable,
    )


def _context_factory(
    seen_assignments: list[Assignment],
) -> Any:
    def build(assignment: Assignment) -> ContextPack:
        seen_assignments.append(assignment.model_copy(deep=True))
        return ContextPack(
            task_id=TASK_ID,
            loop_id=LOOP_ID,
            agent_id=assignment.agent_id,
            mission=assignment.assignment_id,
            phase=LoopPhase.EXPLORE.value,
            target_hunger_item_ids=list(assignment.target_hunger_item_ids),
            candidate_workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
            allowed_tools=list(assignment.allowed_tools),
            budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
        )

    return build


def _save_mission(
    repo: InMemoryRepository,
    *,
    features: list[MissionFeature] | None = None,
) -> None:
    mission_features = features or []
    phases = [
        MissionPhase(
            phase_id=phase_id,
            title=f"Phase {phase_id}",
            description=f"Phase for {phase_id}",
            feature_ids=[
                feature.feature_id
                for feature in mission_features
                if feature.phase_id == phase_id
            ],
        )
        for phase_id in sorted({feature.phase_id for feature in mission_features})
    ]
    repo.save_mission(
        Mission(
            mission_id=MISSION_ID,
            task_id=TASK_ID,
            title="Mission",
            description="Mission scoped assignment event test",
            phases=phases,
            features=mission_features,
            created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        )
    )


def _assert_assignment_events_have_mission_id(
    repo: InMemoryRepository,
    event_types: list[str],
) -> None:
    for event_type in event_types:
        events = repo.list_events(TASK_ID, event_types=[event_type])
        assert events, f"expected {event_type} event"
        for event in events:
            assert event["payload"]["mission_id"] == MISSION_ID


class _RecordingRuntime:
    def __init__(
        self,
        scripted: dict[str, list[WorkerHandoff]],
        *,
        recorder: list[str] | None = None,
    ) -> None:
        self.scripted = {key: list(value) for key, value in scripted.items()}
        self.order: list[str] = []
        self.workspace_roots: list[Path] = []
        self._running = False
        self.overlap_detected = False
        self.recorder = recorder

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerHandoff:
        assert spec.agent_id == context.agent_id
        if self._running:
            self.overlap_detected = True
        self._running = True
        assignment_id = context.mission
        self.order.append(assignment_id)
        self.workspace_roots.append(workspace_root)
        if self.recorder is not None:
            self.recorder.append(f"run:{assignment_id}")
        self._running = False
        queue = self.scripted[assignment_id]
        return queue.pop(0)


class _CountingCostGuard:
    def __init__(
        self,
        *,
        recorder: list[str] | None = None,
        raise_on_call: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.recorder = recorder
        self.raise_on_call = raise_on_call

    def assert_within_budget(self, task_id: str) -> None:
        self.calls.append(task_id)
        if self.recorder is not None:
            self.recorder.append("budget")
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            raise SafetyStopError("cost ceiling")


@pytest.fixture
def repo() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.create_task(TASK_ID, "Goal")
    repository.save_agent_spec(
        AgentSpec(agent_id=AGENT_ID, name="ExecutionWorker", allowed_tools=["write_file"])
    )
    return repository


def _scheduler(
    *,
    repo: InMemoryRepository,
    tmp_path: Path,
    runtime: _RecordingRuntime,
    cost_guard: _CountingCostGuard | None = None,
) -> WorkerScheduler:
    return WorkerScheduler(
        repo=repo,
        worker_runtime=runtime,
        cost_guard=cost_guard or _CountingCostGuard(),
        workspace_manager=WorkspaceManager(tmp_path),
    )


def test_public_signature_and_result_has_no_stop_reason() -> None:
    assert inspect.iscoroutinefunction(WorkerScheduler.execute_assignments)
    assert {field.name for field in fields(SchedulerResult)} == {
        "handoffs",
        "skipped_ids",
        "cycle_detected",
    }
    assert not hasattr(SchedulerResult, "stop_reason")


async def test_topology_execution_serial(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    assignments = [
        _assignment("A"),
        _assignment("B", depends_on=["A"]),
        _assignment("C", depends_on=["B"]),
    ]
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A")],
            "B": [_handoff("B")],
            "C": [_handoff("C")],
        }
    )
    seen_assignments: list[Assignment] = []

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        assignments,
        _context_factory(seen_assignments),
    )

    assert runtime.order == ["A", "B", "C"]
    assert runtime.overlap_detected is False
    assert [handoff.assignment_id for handoff in result.handoffs] == ["A", "B", "C"]
    assert result.skipped_ids == []
    assert all(
        root
        == WorkspaceManager(tmp_path).candidate_files_dir(TASK_ID, LOOP_ID)
        for root in runtime.workspace_roots
    )


async def test_out_of_order_acyclic_assignments_run_after_dependencies(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    assignments = [
        _assignment("B", depends_on=["A"]),
        _assignment("A"),
        _assignment("C", depends_on=["B"]),
    ]
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A")],
            "B": [_handoff("B")],
            "C": [_handoff("C")],
        }
    )

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        assignments,
        _context_factory([]),
    )

    assert runtime.order == ["A", "B", "C"]
    assert result.skipped_ids == []
    assert [handoff.assignment_id for handoff in result.handoffs] == ["A", "B", "C"]
    assert repo.list_events(TASK_ID, event_types=["worker.assignment_skipped"]) == []


async def test_retry_succeeds_with_same_assignment_id_and_persists_audit(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    assignment = _assignment("A", max_retries=1)
    runtime = _RecordingRuntime(
        {"A": [_handoff("A", error_type="timeout", retryable=True), _handoff("A")]}
    )
    seen_assignments: list[Assignment] = []

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        [assignment],
        _context_factory(seen_assignments),
    )

    assert runtime.order == ["A", "A"]
    assert [seen.assignment_id for seen in seen_assignments] == ["A", "A"]
    assert [seen.retry_count for seen in seen_assignments] == [0, 1]
    assert [handoff.retry_count for handoff in result.handoffs] == [1]
    assert [handoff.error_type for handoff in result.handoffs] == [None]
    assert len(repo.list_worker_handoffs(TASK_ID)) == 2

    events = repo.list_events(TASK_ID, event_types=["worker.assignment_started"])
    assert [event["payload"]["attempt"] for event in events] == [1, 2]
    retried = repo.list_events(TASK_ID, event_types=["worker.assignment_retried"])
    assert retried[0]["payload"]["assignment_id"] == "A"
    assert retried[0]["payload"]["attempt"] == 2

    audit_path = (
        WorkspaceManager(tmp_path).task_root(TASK_ID)
        / "candidates"
        / "loop_001"
        / "handoffs"
        / "A.json"
    )
    parsed = WorkerHandoff.model_validate_json(audit_path.read_text(encoding="utf-8"))
    assert parsed.assignment_id == "A"
    assert parsed.retry_count == 1
    assert json.loads(audit_path.read_text(encoding="utf-8"))["assignment_id"] == "A"


async def test_retry_exhaustion_skips_downstream_and_emits_lifecycle_events(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    _save_mission(repo)
    assignments = [
        _assignment("A"),
        _assignment("B", max_retries=1),
        _assignment("C", depends_on=["B"]),
    ]
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A")],
            "B": [
                _handoff("B", error_type="model_call_error", retryable=True),
                _handoff("B", error_type="model_call_error", retryable=True),
            ],
        }
    )

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        assignments,
        _context_factory([]),
    )

    assert runtime.order == ["A", "B", "B"]
    assert result.skipped_ids == ["C"]
    assert [handoff.assignment_id for handoff in result.handoffs] == ["A", "B"]
    assert [handoff.error_type for handoff in result.handoffs] == [
        None,
        "model_call_error",
    ]

    skipped = repo.list_events(TASK_ID, event_types=["worker.assignment_skipped"])
    assert skipped[0]["payload"]["assignment_id"] == "C"
    assert skipped[0]["payload"]["blocked_by"] == "B"
    event_types = {
        str(event["event_type"])
        for event in repo.list_events(TASK_ID, since_loop=LOOP_ID, until_loop=LOOP_ID)
    }
    assert {
        "worker.assignment_started",
        "worker.assignment_completed",
        "worker.assignment_failed",
        "worker.assignment_skipped",
        "worker.assignment_retried",
    } <= event_types
    _assert_assignment_events_have_mission_id(
        repo,
        [
            "worker.assignment_started",
            "worker.assignment_completed",
            "worker.assignment_failed",
            "worker.assignment_skipped",
            "worker.assignment_retried",
        ],
    )

    failed_audit = (
        WorkspaceManager(tmp_path).task_root(TASK_ID)
        / "candidates"
        / "loop_001"
        / "handoffs"
        / "B.json"
    )
    failed_handoff = WorkerHandoff.model_validate_json(
        failed_audit.read_text(encoding="utf-8")
    )
    assert failed_handoff.error_type == "model_call_error"


async def test_prior_invocation_dependency_is_not_skipped_as_pending(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A")],
            "B": [_handoff("B")],
        }
    )
    scheduler = _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime)

    first_result = await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID,
        [_assignment("A")],
        _context_factory([]),
    )
    second_result = await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID + 1,
        [_assignment("B", depends_on=["A"])],
        _context_factory([]),
    )

    assert runtime.order == ["A", "B"]
    assert first_result.skipped_ids == []
    assert second_result.skipped_ids == []
    assert [handoff.assignment_id for handoff in second_result.handoffs] == ["B"]
    assert repo.list_events(TASK_ID, event_types=["worker.assignment_skipped"]) == []


async def test_failure_does_not_skip_independent_siblings(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    assignments = [_assignment("A"), _assignment("B"), _assignment("C")]
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A", error_type="configuration", retryable=False)],
            "B": [_handoff("B")],
            "C": [_handoff("C")],
        }
    )

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        assignments,
        _context_factory([]),
    )

    assert runtime.order == ["A", "B", "C"]
    assert result.skipped_ids == []
    assert [handoff.assignment_id for handoff in result.handoffs] == ["A", "B", "C"]


async def test_cycle_detected_skips_every_assignment(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    _save_mission(repo)
    assignments = [
        _assignment("A", depends_on=["B"]),
        _assignment("B", depends_on=["A"]),
    ]
    runtime = _RecordingRuntime({"A": [_handoff("A")], "B": [_handoff("B")]})

    result = await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        assignments,
        _context_factory([]),
    )

    assert result == SchedulerResult(
        handoffs=[],
        skipped_ids=["A", "B"],
        cycle_detected=True,
    )
    assert runtime.order == []
    _assert_assignment_events_have_mission_id(
        repo,
        ["worker.assignment_skipped"],
    )


async def test_safety_stop_marks_remaining_skipped_and_propagates(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    _save_mission(repo)
    assignments = [_assignment("A"), _assignment("B"), _assignment("C")]
    runtime = _RecordingRuntime(
        {
            "A": [_handoff("A")],
            "B": [_handoff("B")],
            "C": [_handoff("C")],
        }
    )
    cost_guard = _CountingCostGuard(raise_on_call=3)
    scheduler = _scheduler(
        repo=repo,
        tmp_path=tmp_path,
        runtime=runtime,
        cost_guard=cost_guard,
    )

    with pytest.raises(SafetyStopError):
        await scheduler.execute_assignments(
            TASK_ID,
            LOOP_ID,
            assignments,
            _context_factory([]),
        )

    assert runtime.order == ["A"]
    skipped = repo.list_events(TASK_ID, event_types=["worker.assignment_skipped"])
    assert [event["payload"]["assignment_id"] for event in skipped] == ["B", "C"]
    _assert_assignment_events_have_mission_id(
        repo,
        ["worker.assignment_started", "worker.assignment_completed", "worker.assignment_skipped"],
    )
    assert cost_guard.calls == [TASK_ID, TASK_ID, TASK_ID]


async def test_assignment_events_include_scoping_keys(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    _save_mission(
        repo,
        features=[
            MissionFeature(
                feature_id="feature-a",
                hunger_item_id="H-A",
                phase_id="phase-a",
                title="Feature A",
                description="Feature A scoping",
            )
        ],
    )
    assignment = _assignment("A", target_feature_ids=["feature-a"])
    runtime = _RecordingRuntime({"A": [_handoff("A")]})

    await _scheduler(repo=repo, tmp_path=tmp_path, runtime=runtime).execute_assignments(
        TASK_ID,
        LOOP_ID,
        [assignment],
        _context_factory([]),
    )

    started = repo.list_events(TASK_ID, event_types=["worker.assignment_started"])[0]
    completed = repo.list_events(TASK_ID, event_types=["worker.assignment_completed"])[0]
    for event in (started, completed):
        payload = event["payload"]
        assert payload["mission_id"] == MISSION_ID
        assert payload["phase_id"] == "phase-a"
        assert payload["feature_id"] == "feature-a"
        assert payload["assignment_id"] == "A"
        assert payload["target_feature_ids"] == ["feature-a"]
        assert payload["target_hunger_item_ids"] == ["H-A"]
