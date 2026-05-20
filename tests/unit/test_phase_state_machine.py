from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import LoopPhase, StopReason, ValidationVerdict
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.migration_errors import IllegalPhaseTransition
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.validation_pipeline import ValidationPipeline, ValidationPipelineResult

TASK_ID = "task-phase-sm"
MISSION_ID = "mission-phase-sm"
PHASE_ID = "phase-1"
LOOP_ID = 7


def _phase(status: str) -> MissionPhase:
    return MissionPhase(
        phase_id=PHASE_ID,
        title="Implementation phase",
        description="Phase description",
        feature_ids=["feature-1", "feature-2"],
        status=status,
    )


def _feature(feature_id: str, hunger_item_id: str, *, status: str) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id=PHASE_ID,
        title=f"Feature {feature_id}",
        description="Feature description",
        status=status,
    )


def _mission(phase: MissionPhase, features: list[MissionFeature]) -> Mission:
    return Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Mission",
        description="Mission description",
        phases=[phase],
        features=features,
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )


def _repo_with_phase(
    *,
    phase_status: str,
    feature_statuses: list[str],
) -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.create_task(TASK_ID, "phase state machine")
    repo.set_hunger_policy(TASK_ID, HungerPolicy())
    repo.get_hunger_clock(TASK_ID)
    items = [
        HungerItem(id=f"H-{index}", title=f"Item {index}")
        for index, _status in enumerate(feature_statuses, start=1)
    ]
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=items))
    features = [
        _feature(f"feature-{index}", f"H-{index}", status=status)
        for index, status in enumerate(feature_statuses, start=1)
    ]
    repo.save_mission(_mission(_phase(phase_status), features))
    return repo


def _candidate() -> CandidateState:
    return CandidateState(
        id=f"CAND-{TASK_ID}-{LOOP_ID}",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref="candidates/loop_007",
    )


def _validation_report(
    verdict: ValidationVerdict,
    report_id: str,
    *,
    regressed_check_keys: list[str] | None = None,
) -> ValidationReport:
    return ValidationReport(
        id=report_id,
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate_state_id=_candidate().id,
        baseline_state_id=None,
        verdict=verdict,
        regressed_check_keys=list(regressed_check_keys or []),
    )


def _pipeline_result(
    *,
    deterministic: ValidationVerdict = ValidationVerdict.PASS,
    scrutiny: ValidationVerdict | None = ValidationVerdict.PASS,
    user_testing: ValidationVerdict | None = ValidationVerdict.PASS,
    pipeline_verdict: str = "pass",
    regressed_check_keys: list[str] | None = None,
) -> ValidationPipelineResult:
    stages = ["deterministic"]
    scrutiny_report = None
    user_testing_report = None
    if scrutiny is not None:
        stages.append("scrutiny")
        scrutiny_report = _validation_report(scrutiny, "VAL-scrutiny")
    if user_testing is not None:
        stages.append("user_testing")
        user_testing_report = _validation_report(user_testing, "VAL-user-testing")
    return ValidationPipelineResult(
        deterministic_report=_validation_report(
            deterministic,
            "VAL-deterministic",
            regressed_check_keys=regressed_check_keys,
        ),
        scrutiny_report=scrutiny_report,
        user_testing_report=user_testing_report,
        pipeline_verdict=pipeline_verdict,
        stages_run=stages,
    )


def _tick(repo: InMemoryRepository, **kwargs: object) -> None:
    engine = HungerEngine(repo=repo)
    engine.tick(
        repo.get_hunger_policy(TASK_ID),
        repo.get_hunger_clock(TASK_ID),
        repo.get_hunger_ledger(TASK_ID),
        task_id=TASK_ID,
        **kwargs,
    )


def _stored_phase(repo: InMemoryRepository) -> MissionPhase:
    return repo.list_mission_phases(MISSION_ID)[0]


def _event_payloads(repo: InMemoryRepository, event_type: str) -> list[dict[str, object]]:
    return [event["payload"] for event in repo.list_events(TASK_ID, event_types=[event_type])]


def test_pending_to_in_progress_emits_phase_started_once() -> None:
    repo = _repo_with_phase(
        phase_status="pending",
        feature_statuses=["in_progress", "pending"],
    )

    _tick(repo)
    _tick(repo)

    assert _stored_phase(repo).status == "in_progress"
    payloads = _event_payloads(repo, "mission.phase_started")
    assert payloads == [
        {
            "mission_id": MISSION_ID,
            "phase_id": PHASE_ID,
            "previous_status": "pending",
            "new_status": "in_progress",
        }
    ]


def test_in_progress_to_validating() -> None:
    repo = _repo_with_phase(
        phase_status="in_progress",
        feature_statuses=["done", "done"],
    )

    snapshot = HungerEngine(repo=repo).tick(
        repo.get_hunger_policy(TASK_ID),
        repo.get_hunger_clock(TASK_ID),
        repo.get_hunger_ledger(TASK_ID),
        task_id=TASK_ID,
    )

    assert _stored_phase(repo).status == "validating"
    assert snapshot.should_stop is False
    assert snapshot.stop_reason is None
    payloads = _event_payloads(repo, "mission.phase_validation_started")
    assert len(payloads) == 1
    assert payloads[0]["mission_id"] == MISSION_ID
    assert payloads[0]["phase_id"] == PHASE_ID
    assert payloads[0]["previous_status"] == "in_progress"
    assert payloads[0]["new_status"] == "validating"


def test_validating_to_done_requires_both_pipelines() -> None:
    repo = _repo_with_phase(
        phase_status="validating",
        feature_statuses=["done", "done"],
    )

    _tick(
        repo,
        validation_result=_pipeline_result(
            scrutiny=ValidationVerdict.PASS,
            user_testing=None,
            pipeline_verdict="pass",
        ),
        validation_phase_id=PHASE_ID,
    )
    assert _stored_phase(repo).status == "validating"
    assert _event_payloads(repo, "mission.phase_completed") == []

    _tick(
        repo,
        validation_result=_pipeline_result(),
        validation_phase_id=PHASE_ID,
    )

    stored = _stored_phase(repo)
    assert stored.status == "done"
    assert stored.completed_at is not None
    payloads = _event_payloads(repo, "mission.phase_completed")
    assert len(payloads) == 1
    assert payloads[0]["mission_id"] == MISSION_ID
    assert payloads[0]["phase_id"] == PHASE_ID
    assert payloads[0]["previous_status"] == "validating"
    assert payloads[0]["new_status"] == "done"


@pytest.mark.parametrize(
    ("deterministic", "scrutiny", "user_testing"),
    [
        (ValidationVerdict.FAIL, None, None),
        (ValidationVerdict.PASS, ValidationVerdict.FAIL, None),
        (ValidationVerdict.PASS, ValidationVerdict.PASS, ValidationVerdict.FAIL),
    ],
)
def test_validating_to_in_progress_on_failure(
    deterministic: ValidationVerdict,
    scrutiny: ValidationVerdict | None,
    user_testing: ValidationVerdict | None,
) -> None:
    repo = _repo_with_phase(
        phase_status="validating",
        feature_statuses=["done", "done"],
    )

    _tick(
        repo,
        validation_result=_pipeline_result(
            deterministic=deterministic,
            scrutiny=scrutiny,
            user_testing=user_testing,
            pipeline_verdict="fail",
        ),
        validation_phase_id=PHASE_ID,
    )

    assert _stored_phase(repo).status == "in_progress"
    payloads = _event_payloads(repo, "mission.phase_validation_failed")
    assert len(payloads) == 1
    assert payloads[0]["mission_id"] == MISSION_ID
    assert payloads[0]["phase_id"] == PHASE_ID
    assert payloads[0]["previous_status"] == "validating"
    assert payloads[0]["new_status"] == "in_progress"


def test_validating_does_not_complete_with_deterministic_regression() -> None:
    repo = _repo_with_phase(
        phase_status="validating",
        feature_statuses=["done", "done"],
    )

    _tick(
        repo,
        validation_result=_pipeline_result(regressed_check_keys=["H-1:0"]),
        validation_phase_id=PHASE_ID,
    )

    assert _stored_phase(repo).status == "validating"
    assert _event_payloads(repo, "mission.phase_completed") == []


def test_validating_does_not_complete_on_partial_pipeline_verdict() -> None:
    repo = _repo_with_phase(
        phase_status="validating",
        feature_statuses=["done", "done"],
    )

    _tick(
        repo,
        validation_result=_pipeline_result(
            deterministic=ValidationVerdict.PARTIAL,
            scrutiny=ValidationVerdict.PASS,
            user_testing=ValidationVerdict.PASS,
            pipeline_verdict="partial",
        ),
        validation_phase_id=PHASE_ID,
    )

    assert _stored_phase(repo).status == "validating"
    assert _event_payloads(repo, "mission.phase_completed") == []
    assert _event_payloads(repo, "mission.phase_validation_failed") == []


def test_done_not_revertible() -> None:
    repo = _repo_with_phase(
        phase_status="done",
        feature_statuses=["done", "done"],
    )

    with pytest.raises(IllegalPhaseTransition):
        repo.update_phase_status(PHASE_ID, "in_progress")

    assert _stored_phase(repo).status == "done"


class _StageValidator:
    def __init__(self, report: ValidationReport) -> None:
        self.report = report

    async def validate(self, **_kwargs: object) -> ValidationReport:
        return self.report


class _NoopCostGuard:
    def assert_within_budget(self, _task_id: str) -> None:
        return None


async def test_pipeline_does_not_write_phase_status() -> None:
    repo = _repo_with_phase(
        phase_status="validating",
        feature_statuses=["done", "done"],
    )
    phase = _stored_phase(repo)
    pipeline = ValidationPipeline(
        repo=repo,
        cost_guard=_NoopCostGuard(),
        deterministic_validator=_StageValidator(
            _validation_report(ValidationVerdict.FAIL, "VAL-deterministic")
        ),
    )

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-1"],
        mission=repo.get_mission(TASK_ID),
        phase=phase,
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert result.pipeline_verdict == "fail"
    assert _stored_phase(repo).status == "validating"
    assert _event_payloads(repo, "mission.phase_validation_failed") == []


async def test_illegal_phase_transition_maps_to_error_stop_and_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo_with_phase(
        phase_status="done",
        feature_statuses=["done", "done"],
    )
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=tmp_path,
        model_client=DummyModelClient(),
    )

    def _raise_illegal_transition(*_args: object, **_kwargs: object) -> None:
        raise IllegalPhaseTransition("phase done cannot transition")

    monkeypatch.setattr(orchestrator.hunger_engine, "tick", _raise_illegal_transition)

    report = await orchestrator.step(TASK_ID)

    assert isinstance(report, StopReport)
    assert report.stop_reason is StopReason.ERROR
    payloads = _event_payloads(repo, "PHASE_TRANSITION_REJECTED")
    assert len(payloads) == 1
    assert payloads[0]["error_type"] == "IllegalPhaseTransition"
    assert "phase done cannot transition" in str(payloads[0]["error_message"])
