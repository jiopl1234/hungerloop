"""Validation pipeline orchestration for HungerLoop v0.6.

The pipeline implements REQ-M4-001..009. It always runs the deterministic
``ValidationGate`` wrapper first, then conditionally runs boundary validators
only when a mission phase is in ``validating``.
"""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.mission import Mission, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import ValidationContract
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validators.deterministic_validator import DeterministicValidator

ValidationPipelineVerdict = Literal["pass", "fail", "skipped"]
ValidationPipelineStage = Literal["deterministic", "scrutiny", "user_testing"]

_CONTINUE_VERDICTS = {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}


class _DeterministicValidatorProtocol(Protocol):
    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        target_hunger_item_ids: list[str],
    ) -> ValidationReport: ...


class _ScrutinyValidatorProtocol(Protocol):
    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        *,
        contract: ValidationContract,
        phase: MissionPhase,
        budget: BudgetAllocation,
    ) -> ValidationReport: ...


class _UserTestingValidatorProtocol(Protocol):
    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        *,
        contract: ValidationContract,
        phase: MissionPhase,
        budget: BudgetAllocation,
    ) -> ValidationReport: ...


class ValidationPipelineResult(BaseModel):
    """Aggregate result from deterministic, scrutiny, and user-testing stages."""

    model_config = ConfigDict(frozen=True)

    deterministic_report: ValidationReport
    scrutiny_report: ValidationReport | None = None
    user_testing_report: ValidationReport | None = None
    pipeline_verdict: ValidationPipelineVerdict
    stages_run: list[ValidationPipelineStage]


class ValidationPipeline:
    """Run validation stages with phase-boundary dispatch and cost checks."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        cost_guard: CostGuard,
        deterministic_validator: _DeterministicValidatorProtocol,
        scrutiny_validator: _ScrutinyValidatorProtocol | None = None,
        user_testing_validator: _UserTestingValidatorProtocol | None = None,
    ) -> None:
        self.repo = repo
        self.cost_guard = cost_guard
        self.deterministic_validator = deterministic_validator
        self.scrutiny_validator = scrutiny_validator
        self.user_testing_validator = user_testing_validator

    @classmethod
    def from_validation_gate(
        cls,
        *,
        repo: RepositoryProtocol,
        cost_guard: CostGuard,
        validation_gate: ValidationGate,
        scrutiny_validator: _ScrutinyValidatorProtocol | None = None,
        user_testing_validator: _UserTestingValidatorProtocol | None = None,
    ) -> ValidationPipeline:
        """Build a pipeline from the legacy ``ValidationGate`` dependency."""
        return cls(
            repo=repo,
            cost_guard=cost_guard,
            deterministic_validator=DeterministicValidator(validation_gate),
            scrutiny_validator=scrutiny_validator,
            user_testing_validator=user_testing_validator,
        )

    async def run(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        target_hunger_item_ids: list[str],
        *,
        mission: Mission | None,
        phase: MissionPhase | None,
        budget: BudgetAllocation,
    ) -> ValidationPipelineResult:
        """Run the M4 pipeline for one candidate.

        ``SafetyStopError`` from the cost guard is intentionally not caught here;
        the orchestrator owns mapping it to ``StopReason.SAFETY_STOP``.
        """
        self._emit_pipeline_started(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            mission=mission,
            phase=phase,
            target_hunger_item_ids=target_hunger_item_ids,
        )

        stages_run: list[ValidationPipelineStage] = []
        deterministic_report = await self._run_deterministic_stage(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            target_hunger_item_ids=target_hunger_item_ids,
        )
        stages_run.append("deterministic")

        scrutiny_report: ValidationReport | None = None
        user_testing_report: ValidationReport | None = None

        should_run_boundary_validators = (
            mission is not None and phase is not None and phase.status == "validating"
        )
        if not should_run_boundary_validators:
            result = ValidationPipelineResult(
                deterministic_report=deterministic_report,
                scrutiny_report=None,
                user_testing_report=None,
                pipeline_verdict=self._pipeline_verdict_from_validation(
                    deterministic_report.verdict
                ),
                stages_run=stages_run,
            )
            self._emit_pipeline_completed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                result=result,
            )
            return result
        assert mission is not None
        assert phase is not None

        if deterministic_report.verdict not in _CONTINUE_VERDICTS:
            self._emit_scrutiny_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="deterministic_failed",
            )
            self._emit_user_testing_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="deterministic_failed",
            )
            result = ValidationPipelineResult(
                deterministic_report=deterministic_report,
                scrutiny_report=None,
                user_testing_report=None,
                pipeline_verdict="fail",
                stages_run=stages_run,
            )
            self._emit_pipeline_completed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                result=result,
            )
            return result

        if self.scrutiny_validator is None:
            self._emit_scrutiny_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="scrutiny_validator_unavailable",
            )
            self._emit_user_testing_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="scrutiny_validator_unavailable",
            )
            result = ValidationPipelineResult(
                deterministic_report=deterministic_report,
                scrutiny_report=None,
                user_testing_report=None,
                pipeline_verdict="skipped",
                stages_run=stages_run,
            )
            self._emit_pipeline_completed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                result=result,
            )
            return result

        boundary_contract = self._contract_for_mission(mission)
        scrutiny_report = await self._run_scrutiny_stage(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            mission=mission,
            phase=phase,
            budget=budget,
            contract=boundary_contract,
        )
        stages_run.append("scrutiny")

        if scrutiny_report.verdict not in _CONTINUE_VERDICTS:
            self._emit_user_testing_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="scrutiny_failed",
            )
            result = ValidationPipelineResult(
                deterministic_report=deterministic_report,
                scrutiny_report=scrutiny_report,
                user_testing_report=None,
                pipeline_verdict="fail",
                stages_run=stages_run,
            )
            self._emit_pipeline_completed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                result=result,
            )
            return result

        if self.user_testing_validator is None:
            self._emit_user_testing_skipped(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                reason="user_testing_validator_unavailable",
            )
            result = ValidationPipelineResult(
                deterministic_report=deterministic_report,
                scrutiny_report=scrutiny_report,
                user_testing_report=None,
                pipeline_verdict="skipped",
                stages_run=stages_run,
            )
            self._emit_pipeline_completed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                result=result,
            )
            return result

        user_testing_report = await self._run_user_testing_stage(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            mission=mission,
            phase=phase,
            budget=budget,
            contract=boundary_contract,
        )
        stages_run.append("user_testing")

        result = ValidationPipelineResult(
            deterministic_report=deterministic_report,
            scrutiny_report=scrutiny_report,
            user_testing_report=user_testing_report,
            pipeline_verdict=self._pipeline_verdict_from_validation(
                user_testing_report.verdict
            ),
            stages_run=stages_run,
        )
        self._emit_pipeline_completed(
            task_id=task_id,
            loop_id=loop_id,
            mission=mission,
            phase=phase,
            result=result,
        )
        return result

    async def _run_deterministic_stage(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        target_hunger_item_ids: list[str],
    ) -> ValidationReport:
        self.cost_guard.assert_within_budget(task_id)
        report = await self.deterministic_validator.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            target_hunger_item_ids=target_hunger_item_ids,
        )
        self.cost_guard.assert_within_budget(task_id)
        return report

    async def _run_scrutiny_stage(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        mission: Mission,
        phase: MissionPhase,
        budget: BudgetAllocation,
        contract: ValidationContract,
    ) -> ValidationReport:
        assert self.scrutiny_validator is not None
        self.cost_guard.assert_within_budget(task_id)
        self._emit_scrutiny_started(
            task_id=task_id,
            loop_id=loop_id,
            mission=mission,
            phase=phase,
        )
        report = await self.scrutiny_validator.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            contract=contract,
            phase=phase,
            budget=budget,
        )
        self.cost_guard.assert_within_budget(task_id)
        self._emit_scrutiny_completed(
            task_id=task_id,
            loop_id=loop_id,
            mission=mission,
            phase=phase,
            report=report,
        )
        return report

    async def _run_user_testing_stage(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        mission: Mission,
        phase: MissionPhase,
        budget: BudgetAllocation,
        contract: ValidationContract,
    ) -> ValidationReport:
        assert self.user_testing_validator is not None
        self.cost_guard.assert_within_budget(task_id)
        self._emit_user_testing_started(
            task_id=task_id,
            loop_id=loop_id,
            mission=mission,
            phase=phase,
        )
        report = await self.user_testing_validator.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            contract=contract,
            phase=phase,
            budget=budget,
        )
        self.cost_guard.assert_within_budget(task_id)
        self._emit_user_testing_completed(
            task_id=task_id,
            loop_id=loop_id,
            mission=mission,
            phase=phase,
            report=report,
        )
        if report.verdict == ValidationVerdict.FAIL:
            self._emit_user_testing_failed(
                task_id=task_id,
                loop_id=loop_id,
                mission=mission,
                phase=phase,
                report=report,
            )
        return report

    def _contract_for_mission(self, mission: Mission) -> ValidationContract:
        return self.repo.get_validation_contract(mission.mission_id) or ValidationContract(
            mission_id=mission.mission_id
        )

    @staticmethod
    def _pipeline_verdict_from_validation(
        verdict: ValidationVerdict,
    ) -> ValidationPipelineVerdict:
        if verdict == ValidationVerdict.PASS:
            return "pass"
        if verdict == ValidationVerdict.PARTIAL:
            return "pass"
        return "fail"

    @staticmethod
    def _base_payload(
        *,
        mission: Mission | None,
        phase: MissionPhase | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        if mission is not None:
            payload["mission_id"] = mission.mission_id
        if phase is not None:
            payload["phase_id"] = phase.phase_id
            payload["phase_status"] = phase.status
        return payload

    def _emit_pipeline_started(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        mission: Mission | None,
        phase: MissionPhase | None,
        target_hunger_item_ids: list[str],
    ) -> None:
        payload = {
            **self._base_payload(mission=mission, phase=phase),
            "candidate_state_id": candidate.id,
            "target_hunger_item_ids": list(target_hunger_item_ids),
        }
        self.repo.append_event(
            EventType.VALIDATION_PIPELINE_STARTED,
            payload,
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_pipeline_completed(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission | None,
        phase: MissionPhase | None,
        result: ValidationPipelineResult,
    ) -> None:
        payload = {
            **self._base_payload(mission=mission, phase=phase),
            "pipeline_verdict": result.pipeline_verdict,
            "stages_run": list(result.stages_run),
            "deterministic_report_id": result.deterministic_report.id,
            "scrutiny_report_id": (
                result.scrutiny_report.id if result.scrutiny_report is not None else None
            ),
            "user_testing_report_id": (
                result.user_testing_report.id
                if result.user_testing_report is not None
                else None
            ),
        }
        self.repo.append_event(
            EventType.VALIDATION_PIPELINE_COMPLETED,
            payload,
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_scrutiny_started(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_SCRUTINY_STARTED,
            self._base_payload(mission=mission, phase=phase),
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_scrutiny_completed(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
        report: ValidationReport,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_SCRUTINY_COMPLETED,
            {
                **self._base_payload(mission=mission, phase=phase),
                "validation_report_id": report.id,
                "verdict": report.verdict.value,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_scrutiny_skipped(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
        reason: str,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_SCRUTINY_SKIPPED,
            {
                **self._base_payload(mission=mission, phase=phase),
                "reason": reason,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_user_testing_skipped(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
        reason: str,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_SKIPPED,
            {
                **self._base_payload(mission=mission, phase=phase),
                "reason": reason,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_user_testing_started(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_STARTED,
            self._base_payload(mission=mission, phase=phase),
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_user_testing_completed(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
        report: ValidationReport,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_COMPLETED,
            {
                **self._base_payload(mission=mission, phase=phase),
                "validation_report_id": report.id,
                "verdict": report.verdict.value,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_user_testing_failed(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission: Mission,
        phase: MissionPhase,
        report: ValidationReport,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_FAILED,
            {
                **self._base_payload(mission=mission, phase=phase),
                "validation_report_id": report.id,
                "verdict": report.verdict.value,
                "missing_evidence": list(report.missing_evidence),
                "recommended_next_actions": list(report.recommended_next_actions),
            },
            task_id=task_id,
            loop_id=loop_id,
        )
