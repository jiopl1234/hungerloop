from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.models.events import EventType
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.migration_errors import IllegalPhaseTransition
from hungerloop.services.model_client import DummyModelClient


def test_event_type_enum_membership() -> None:
    required_members = {
        "MISSION_CREATED": "mission.created",
        "MISSION_PHASE_STARTED": "mission.phase_started",
        "MISSION_PHASE_VALIDATED": "mission.phase_validated",
        "MISSION_PHASE_COMPLETED": "mission.phase_completed",
        "MISSION_FEATURE_ASSIGNED": "mission.feature_assigned",
        "MISSION_FEATURE_COMPLETED": "mission.feature_completed",
        "MISSION_FEATURE_BLOCKED": "mission.feature_blocked",
        "WORKER_HANDOFF_EMITTED": "worker.handoff_emitted",
        "WORKER_HANDOFF_RECEIVED": "worker.handoff_received",
        "WORKER_ASSIGNMENT_STARTED": "worker.assignment_started",
        "WORKER_ASSIGNMENT_COMPLETED": "worker.assignment_completed",
        "WORKER_ASSIGNMENT_FAILED": "worker.assignment_failed",
        "WORKER_ASSIGNMENT_SKIPPED": "worker.assignment_skipped",
        "VALIDATION_SCRUTINY_STARTED": "validation.scrutiny_started",
        "VALIDATION_SCRUTINY_COMPLETED": "validation.scrutiny_completed",
        "VALIDATION_USER_TESTING_STARTED": "validation.user_testing_started",
        "MISSION_STATE_REGENERATED": "mission.state_regenerated",
        "VALIDATION_ASSERTION_PASSED": "validation.assertion_passed",
        "VALIDATION_ASSERTION_FAILED": "validation.assertion_failed",
    }

    for member_name, value in required_members.items():
        assert getattr(EventType, member_name).value == value


def test_loop_trace_v0_6_fields_default_for_legacy() -> None:
    legacy_trace = LoopTrace(
        task_id="legacy-task",
        loop_id=1,
        phase="explore",
        active_hunger=1.0,
        drive_budget=1.0,
        work_pressure=1.0,
        committed=False,
    )

    dumped = legacy_trace.model_dump()
    assert {"mission_snapshot", "assignment_traces", "validation_pipeline_trace"} <= set(
        dumped
    )
    assert dumped["mission_snapshot"] is None
    assert dumped["assignment_traces"] == []
    assert dumped["validation_pipeline_trace"] is None


async def test_illegal_phase_transition_maps_to_error_with_phase_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRepository()
    task_id = "task-illegal-phase"
    phase_id = "phase-1"
    repo.create_task(task_id, "phase transition")
    phase = MissionPhase(
        phase_id=phase_id,
        title="Phase",
        description="Phase description",
        status="done",
    )
    feature = MissionFeature(
        feature_id="feature-1",
        hunger_item_id="H-001",
        phase_id=phase_id,
        title="Feature",
        description="Feature description",
        status="done",
    )
    repo.save_mission(
        Mission(
            mission_id="mission-1",
            task_id=task_id,
            title="Mission",
            description="Mission description",
            phases=[phase],
            features=[feature],
            created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        )
    )
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=tmp_path,
        model_client=DummyModelClient(),
    )

    def _raise_illegal_transition(*_args: object, **_kwargs: object) -> None:
        raise IllegalPhaseTransition(
            "phase done cannot transition",
            phase_id=phase_id,
            from_status="done",
            to_status="in_progress",
        )

    monkeypatch.setattr(orchestrator.hunger_engine, "tick", _raise_illegal_transition)

    report = await orchestrator.step(task_id)

    assert isinstance(report, StopReport)
    assert report.stop_reason is StopReason.ERROR
    rejected_events = repo.list_events(
        task_id,
        event_types=[EventType.PHASE_TRANSITION_REJECTED.value],
    )
    assert len(rejected_events) == 1
    payload = rejected_events[0]["payload"]
    assert payload["phase_id"] == phase_id
    assert payload["from_status"] == "done"
    assert payload["to_status"] == "in_progress"


