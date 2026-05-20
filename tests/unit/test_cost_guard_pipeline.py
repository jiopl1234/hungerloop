from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.mission import Mission, MissionPhase
from hungerloop.models.planning import Assignment, BudgetAllocation
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import AgentSpec, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import SafetyStopError
from hungerloop.services.validation_pipeline import ValidationPipeline
from hungerloop.services.worker_scheduler import WorkerScheduler
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
LOOP_ID = 1
AGENT_ID = "execution_worker_v1"


class _RecordingCostGuard:
    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder
        self.calls: list[str] = []

    def assert_within_budget(self, task_id: str) -> None:
        self.calls.append(task_id)
        self.recorder.append("budget")


class _Runtime:
    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerHandoff:
        self.recorder.append(f"run:{context.mission}")
        return WorkerHandoff(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=f"completed {context.mission}",
        )


class _ValidationStage:
    def __init__(self, report: ValidationReport, recorder: list[str], name: str) -> None:
        self.report = report
        self.recorder = recorder
        self.name = name
        self.calls = 0

    async def validate(self, **_kwargs: object) -> ValidationReport:
        self.calls += 1
        self.recorder.append(f"validate:{self.name}")
        return self.report


@pytest.fixture
def repo() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.create_task(TASK_ID, "Goal")
    repository.save_agent_spec(AgentSpec(agent_id=AGENT_ID, name="ExecutionWorker"))
    return repository


def _assignment(assignment_id: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        agent_id=AGENT_ID,
        mission=assignment_id,
        target_hunger_item_ids=[f"H-{assignment_id}"],
        allowed_tools=[],
    )


def _context(assignment: Assignment) -> ContextPack:
    return ContextPack(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        agent_id=assignment.agent_id,
        mission=assignment.assignment_id,
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=list(assignment.target_hunger_item_ids),
        candidate_workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-1",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=["candidate-ev"],
    )


def _mission() -> Mission:
    return Mission(
        mission_id="mission-1",
        task_id=TASK_ID,
        title="Mission",
        description="Mission description",
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )


def _phase(status: Literal["validating"] = "validating") -> MissionPhase:
    return MissionPhase(
        phase_id="phase-1",
        title="Phase",
        description="Phase description",
        status=status,
    )


def _validation_report(report_id: str, verdict: ValidationVerdict) -> ValidationReport:
    return ValidationReport(
        id=report_id,
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate_state_id="CAND-task-1-1",
        baseline_state_id=None,
        verdict=verdict,
        attempted_hunger_item_ids=["H-001"],
        evidence_ids=["candidate-ev"],
    )


async def test_scheduler_pre_post_calls(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    recorder: list[str] = []
    cost_guard = _RecordingCostGuard(recorder)
    runtime = _Runtime(recorder)
    scheduler = WorkerScheduler(
        repo=repo,
        worker_runtime=runtime,
        cost_guard=cost_guard,
        workspace_manager=WorkspaceManager(tmp_path),
    )

    await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID,
        [_assignment("A"), _assignment("B")],
        _context,
    )

    assert cost_guard.calls == [TASK_ID, TASK_ID, TASK_ID, TASK_ID]
    assert recorder == ["budget", "run:A", "budget", "budget", "run:B", "budget"]


async def test_validator_pre_post_calls(repo: InMemoryRepository) -> None:
    recorder: list[str] = []
    cost_guard = _RecordingCostGuard(recorder)
    deterministic = _ValidationStage(
        _validation_report("VAL-deterministic", ValidationVerdict.PASS),
        recorder,
        "deterministic",
    )
    scrutiny = _ValidationStage(
        _validation_report("VAL-scrutiny", ValidationVerdict.PASS),
        recorder,
        "scrutiny",
    )
    user_testing = _ValidationStage(
        _validation_report("VAL-user-testing", ValidationVerdict.PASS),
        recorder,
        "user_testing",
    )
    pipeline = ValidationPipeline(
        repo=repo,
        cost_guard=cost_guard,
        deterministic_validator=deterministic,
        scrutiny_validator=scrutiny,
        user_testing_validator=user_testing,
    )

    await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase(),
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert cost_guard.calls == [TASK_ID] * 6
    assert recorder == [
        "budget",
        "validate:deterministic",
        "budget",
        "budget",
        "validate:scrutiny",
        "budget",
        "budget",
        "validate:user_testing",
        "budget",
    ]


async def test_mid_pipeline_safety_stop_skips_remaining_stages(
    repo: InMemoryRepository,
) -> None:
    recorder: list[str] = []
    cost_guard = _RecordingCostGuard(recorder)
    deterministic = _ValidationStage(
        _validation_report("VAL-deterministic", ValidationVerdict.PASS),
        recorder,
        "deterministic",
    )
    scrutiny = _ValidationStage(
        _validation_report("VAL-scrutiny", ValidationVerdict.PASS),
        recorder,
        "scrutiny",
    )
    pipeline = ValidationPipeline(
        repo=repo,
        cost_guard=cost_guard,
        deterministic_validator=deterministic,
        scrutiny_validator=scrutiny,
    )

    def raise_on_third_call(task_id: str) -> None:
        cost_guard.calls.append(task_id)
        recorder.append("budget")
        if len(cost_guard.calls) == 3:
            raise SafetyStopError("cost ceiling")

    cost_guard.assert_within_budget = raise_on_third_call  # type: ignore[method-assign]

    with pytest.raises(SafetyStopError):
        await pipeline.run(
            TASK_ID,
            LOOP_ID,
            _candidate(),
            ["H-001"],
            mission=_mission(),
            phase=_phase(),
            budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
        )

    assert cost_guard.calls == [TASK_ID, TASK_ID, TASK_ID]
    assert scrutiny.calls == 0
    assert recorder == ["budget", "validate:deterministic", "budget", "budget"]
