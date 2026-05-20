from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, get_args

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.mission import Mission, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.cost_guard import SafetyStopError
from hungerloop.services.validation_pipeline import (
    ValidationPipeline,
    ValidationPipelineVerdict,
)

TASK_ID = "task-1"
LOOP_ID = 4


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-4",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref="candidates/loop_004",
        evidence_ids=["candidate-ev"],
    )


def _report(
    verdict: ValidationVerdict,
    *,
    report_id: str,
) -> ValidationReport:
    return ValidationReport(
        id=report_id,
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate_state_id="CAND-task-1-4",
        baseline_state_id=None,
        verdict=verdict,
        attempted_hunger_item_ids=["H-001"],
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


def _phase(status: Literal["pending", "in_progress", "validating", "done"]) -> MissionPhase:
    return MissionPhase(
        phase_id="phase-1",
        title="Phase",
        description="Phase description",
        status=status,
    )


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


class _RecordingCostGuard:
    def __init__(self, *, raise_on_call: int | None = None) -> None:
        self.calls: list[str] = []
        self.raise_on_call = raise_on_call

    def assert_within_budget(self, task_id: str) -> None:
        self.calls.append(task_id)
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            raise SafetyStopError("cost ceiling")


class _StageValidator:
    def __init__(self, report: ValidationReport, stage_name: str) -> None:
        self.report = report
        self.stage_name = stage_name
        self.calls: list[dict[str, object]] = []

    async def validate(self, **kwargs: object) -> ValidationReport:
        self.calls.append(dict(kwargs))
        return self.report


def _pipeline(
    *,
    deterministic_verdict: ValidationVerdict = ValidationVerdict.PASS,
    scrutiny_verdict: ValidationVerdict = ValidationVerdict.PASS,
    cost_guard: _RecordingCostGuard | None = None,
) -> tuple[
    ValidationPipeline,
    InMemoryRepository,
    _StageValidator,
    _StageValidator,
    _RecordingCostGuard,
]:
    repo = InMemoryRepository()
    guard = cost_guard or _RecordingCostGuard()
    deterministic = _StageValidator(
        _report(deterministic_verdict, report_id="VAL-deterministic"),
        "deterministic",
    )
    scrutiny = _StageValidator(
        _report(scrutiny_verdict, report_id="VAL-scrutiny"),
        "scrutiny",
    )
    pipeline = ValidationPipeline(
        repo=repo,
        cost_guard=guard,
        deterministic_validator=deterministic,
        scrutiny_validator=scrutiny,
    )
    return pipeline, repo, deterministic, scrutiny, guard


async def test_legacy_path_runs_only_deterministic() -> None:
    pipeline, repo, deterministic, scrutiny, guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PARTIAL,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=None,
        phase=None,
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic"]
    assert result.pipeline_verdict == "pass"
    assert result.deterministic_report.verdict == ValidationVerdict.PARTIAL
    assert result.scrutiny_report is None
    assert result.user_testing_report is None
    assert len(deterministic.calls) == 1
    assert scrutiny.calls == []
    assert guard.calls == [TASK_ID, TASK_ID]
    event_types = {event["event_type"] for event in repo.list_events(TASK_ID)}
    assert "validation.pipeline_started" in event_types
    assert "validation.pipeline_completed" in event_types
    assert not any(str(event_type).startswith("validation.scrutiny_") for event_type in event_types)
    assert "validation.user_testing_started" not in event_types


def test_pipeline_verdict_literal_matches_m4_spec() -> None:
    assert set(get_args(ValidationPipelineVerdict)) == {"pass", "fail", "skipped"}


async def test_in_progress_skips_scrutiny() -> None:
    pipeline, repo, _deterministic, scrutiny, guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PASS,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("in_progress"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic"]
    assert result.pipeline_verdict == "pass"
    assert scrutiny.calls == []
    assert guard.calls == [TASK_ID, TASK_ID]
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_started"]) == []


async def test_validating_boundary_without_scrutiny_validator_returns_skipped() -> None:
    repo = InMemoryRepository()
    guard = _RecordingCostGuard()
    deterministic = _StageValidator(
        _report(ValidationVerdict.PASS, report_id="VAL-deterministic"),
        "deterministic",
    )
    pipeline = ValidationPipeline(
        repo=repo,
        cost_guard=guard,
        deterministic_validator=deterministic,
        scrutiny_validator=None,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic"]
    assert result.pipeline_verdict == "skipped"
    assert result.scrutiny_report is None
    assert result.user_testing_report is None
    assert guard.calls == [TASK_ID, TASK_ID]
    skipped = repo.list_events(TASK_ID, event_types=["validation.scrutiny_skipped"])
    assert skipped
    assert skipped[0]["payload"]["reason"] == "scrutiny_validator_unavailable"
    user_testing_skipped = repo.list_events(
        TASK_ID,
        event_types=["validation.user_testing_skipped"],
    )
    assert user_testing_skipped
    assert user_testing_skipped[0]["payload"]["reason"] == "scrutiny_validator_unavailable"


async def test_scrutiny_runs_after_deterministic_pass() -> None:
    pipeline, repo, _deterministic, scrutiny, guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PASS,
        scrutiny_verdict=ValidationVerdict.PASS,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic", "scrutiny"]
    assert result.pipeline_verdict == "skipped"
    assert result.scrutiny_report is not None
    assert len(scrutiny.calls) == 1
    assert guard.calls == [TASK_ID, TASK_ID, TASK_ID, TASK_ID]
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_started"])
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_completed"])
    user_testing_skipped = repo.list_events(
        TASK_ID,
        event_types=["validation.user_testing_skipped"],
    )
    assert user_testing_skipped
    assert user_testing_skipped[0]["payload"]["reason"] == "user_testing_validator_unavailable"


async def test_deterministic_partial_allows_scrutiny() -> None:
    pipeline, _repo, _deterministic, scrutiny, _guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PARTIAL,
        scrutiny_verdict=ValidationVerdict.PASS,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic", "scrutiny"]
    assert len(scrutiny.calls) == 1


async def test_fail_short_circuits_scrutiny() -> None:
    pipeline, repo, _deterministic, scrutiny, guard = _pipeline(
        deterministic_verdict=ValidationVerdict.FAIL,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic"]
    assert result.pipeline_verdict == "fail"
    assert result.scrutiny_report is None
    assert result.user_testing_report is None
    assert scrutiny.calls == []
    assert guard.calls == [TASK_ID, TASK_ID]
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_skipped"])
    user_testing_skipped = repo.list_events(
        TASK_ID,
        event_types=["validation.user_testing_skipped"],
    )
    assert user_testing_skipped
    assert user_testing_skipped[0]["payload"]["reason"] == "deterministic_failed"


async def test_scrutiny_fail_skips_user_testing() -> None:
    pipeline, repo, _deterministic, _scrutiny, _guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PASS,
        scrutiny_verdict=ValidationVerdict.FAIL,
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic", "scrutiny"]
    assert result.pipeline_verdict == "fail"
    user_testing_skipped = repo.list_events(
        TASK_ID,
        event_types=["validation.user_testing_skipped"],
    )
    assert user_testing_skipped
    assert user_testing_skipped[0]["payload"]["reason"] == "scrutiny_failed"
    assert repo.list_events(TASK_ID, event_types=["validation.user_testing_started"]) == []


async def test_user_testing_runs_after_scrutiny_pass_when_configured() -> None:
    pipeline, _repo, _deterministic, _scrutiny, guard = _pipeline(
        deterministic_verdict=ValidationVerdict.PASS,
        scrutiny_verdict=ValidationVerdict.PASS,
    )
    user_testing = _StageValidator(
        _report(ValidationVerdict.PASS, report_id="VAL-user-testing"),
        "user_testing",
    )
    pipeline.user_testing_validator = user_testing

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    assert result.stages_run == ["deterministic", "scrutiny", "user_testing"]
    assert result.pipeline_verdict == "pass"
    assert result.user_testing_report is not None
    assert len(user_testing.calls) == 1
    assert guard.calls == [TASK_ID, TASK_ID, TASK_ID, TASK_ID, TASK_ID, TASK_ID]


async def test_pipeline_lifecycle_events() -> None:
    pipeline, repo, _deterministic, _scrutiny, _guard = _pipeline()

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=_mission(),
        phase=_phase("validating"),
        budget=_budget(),
    )

    started = repo.list_events(TASK_ID, event_types=["validation.pipeline_started"])
    completed = repo.list_events(TASK_ID, event_types=["validation.pipeline_completed"])
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0]["loop_id"] == LOOP_ID
    assert completed[0]["loop_id"] == LOOP_ID
    assert completed[0]["payload"]["pipeline_verdict"] == result.pipeline_verdict
    assert completed[0]["payload"]["stages_run"] == ["deterministic", "scrutiny"]


async def test_safety_stop_mid_pipeline_propagates_and_skips_downstream() -> None:
    guard = _RecordingCostGuard(raise_on_call=3)
    pipeline, repo, _deterministic, scrutiny, _guard = _pipeline(cost_guard=guard)

    with pytest.raises(SafetyStopError):
        await pipeline.run(
            TASK_ID,
            LOOP_ID,
            _candidate(),
            ["H-001"],
            mission=_mission(),
            phase=_phase("validating"),
            budget=_budget(),
        )

    assert guard.calls == [TASK_ID, TASK_ID, TASK_ID]
    assert scrutiny.calls == []
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_started"]) == []
    assert repo.list_events(TASK_ID, event_types=["validation.pipeline_completed"]) == []
