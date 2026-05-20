"""User-testing validation stage for HungerLoop v0.6."""
from __future__ import annotations

import re
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import EvidenceType, ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.mission import MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationContract,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.validators.user_testing_predicates import (
    MalformedPredicateParams,
    UserTestingPredicateContext,
    UserTestingPredicateResult,
    get_user_testing_predicate,
)
from hungerloop.services.workspace_manager import WorkspaceManager

_MAX_EVENT_OUTPUT_CHARS = 5000


class UserTestingValidator:
    """Run deterministic validation-contract predicates at phase boundaries."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        sandbox_runner: SandboxRunner,
        workspace_manager: WorkspaceManager,
    ) -> None:
        self.repo = repo
        self.sandbox_runner = sandbox_runner
        self.workspace_manager = workspace_manager

    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        *,
        contract: ValidationContract,
        phase: MissionPhase,
        budget: BudgetAllocation,
    ) -> ValidationReport:
        """Run user-testing assertions for ``phase.phase_id`` only."""
        candidate_root = resolve_workspace_path(
            self.workspace_manager.root,
            f"tasks/{task_id}/candidates/loop_{loop_id:03d}/files",
        )
        assertions = contract.assertions_by_phase(phase.phase_id)
        evidence_ids: list[str] = []
        missing_evidence: list[str] = []
        evaluated_assertions: list[ValidationAssertion] = []
        event_assertions: list[dict[str, object]] = []
        emit_lifecycle_events = not self._has_user_testing_event(
            task_id,
            loop_id,
            EventType.VALIDATION_USER_TESTING_STARTED.value,
        )

        if emit_lifecycle_events:
            self._emit_started(
                task_id=task_id,
                loop_id=loop_id,
                contract=contract,
                phase=phase,
                assertion_ids=[assertion.assertion_id for assertion in assertions],
            )

        for assertion in assertions:
            result = await self._run_assertion(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                contract=contract,
                phase=phase,
                assertion=assertion,
                candidate_root=candidate_root,
            )
            predicate_evidence_ids = self._persist_result(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                contract=contract,
                phase=phase,
                assertion=assertion,
                result=result,
            )
            evidence_ids.extend(predicate_evidence_ids)
            evidence_ids.extend(result.supporting_evidence_ids)
            missing_evidence.extend(result.missing_evidence)
            missing_evidence.extend(
                self._missing_prior_evidence_ids(task_id, assertion)
            )

            updated_assertion = assertion.model_copy(
                update={
                    "status": result.status,
                    "validated_at_loop": loop_id,
                    "evidence_ids": predicate_evidence_ids,
                }
            )
            evaluated_assertions.append(updated_assertion)
            event_assertions.append(
                {
                    "assertion_id": assertion.assertion_id,
                    "check_type": assertion.check_type,
                    "status": result.status,
                    "detail": result.detail,
                    "evidence_ids": list(predicate_evidence_ids),
                }
            )
            if result.timed_out:
                self._emit_timeout(
                    task_id=task_id,
                    loop_id=loop_id,
                    contract=contract,
                    phase=phase,
                    assertion=assertion,
                    result=result,
                )

        verdict = self._decide_verdict(evaluated_assertions, missing_evidence)
        report = ValidationReport(
            id=f"VAL-user-testing-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            candidate_state_id=candidate.id,
            baseline_state_id=None,
            verdict=verdict,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            missing_evidence=list(dict.fromkeys(missing_evidence)),
            recommended_next_actions=self._recommended_next_actions(
                evaluated_assertions
            ),
            has_real_progress=any(
                assertion.status == "passed" for assertion in evaluated_assertions
            ),
        )
        if emit_lifecycle_events:
            self._emit_completed(
                task_id=task_id,
                loop_id=loop_id,
                contract=contract,
                phase=phase,
                report=report,
                assertions=event_assertions,
            )
        return report

    async def _run_assertion(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        contract: ValidationContract,
        phase: MissionPhase,
        assertion: ValidationAssertion,
        candidate_root: Path,
    ) -> UserTestingPredicateResult:
        predicate = get_user_testing_predicate(assertion.check_type)
        if predicate is None:
            return UserTestingPredicateResult(
                status="failed",
                detail="unknown_check_type",
                evidence_payload={"known": False},
            )
        try:
            return await predicate(
                UserTestingPredicateContext(
                    task_id=task_id,
                    loop_id=loop_id,
                    candidate=candidate,
                    assertion=assertion,
                    candidate_root=candidate_root,
                    repo=self.repo,
                    sandbox_runner=self.sandbox_runner,
                    default_timeout_seconds=60,
                )
            )
        except MalformedPredicateParams:
            return UserTestingPredicateResult(
                status="failed",
                detail="malformed_params",
                evidence_payload={"malformed_params": True},
            )
        except (OSError, UnicodeError, re.error) as exc:
            return UserTestingPredicateResult(
                status="failed",
                detail="malformed_params",
                evidence_payload={"malformed_params": True, "error": str(exc)},
            )

    def _persist_result(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        contract: ValidationContract,
        phase: MissionPhase,
        assertion: ValidationAssertion,
        result: UserTestingPredicateResult,
    ) -> list[str]:
        supporting_evidence_ids = list(dict.fromkeys(result.supporting_evidence_ids))
        evidence_id = self.repo.save_evidence(
            task_id=task_id,
            loop_id=loop_id,
            evidence_type=EvidenceType.VALIDATION_CHECK,
            payload={
                **self._event_payload(contract=contract, phase=phase),
                "candidate_state_id": candidate.id,
                "assertion_id": assertion.assertion_id,
                "check_type": assertion.check_type,
                "status": result.status,
                "detail": result.detail,
                "success": result.status == "passed",
                "evidence_kind": "user_testing_predicate",
                "supporting_evidence_ids": supporting_evidence_ids,
                **result.evidence_payload,
            },
        )
        evidence_ids = [evidence_id, *supporting_evidence_ids]
        self.repo.update_assertion_status(
            assertion.assertion_id,
            result.status,
            validated_at_loop=loop_id,
            evidence_ids=evidence_ids,
        )
        return evidence_ids

    def _emit_started(
        self,
        *,
        task_id: str,
        loop_id: int,
        contract: ValidationContract,
        phase: MissionPhase,
        assertion_ids: list[str],
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_STARTED,
            {
                **self._event_payload(contract=contract, phase=phase),
                "assertion_ids": assertion_ids,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_completed(
        self,
        *,
        task_id: str,
        loop_id: int,
        contract: ValidationContract,
        phase: MissionPhase,
        report: ValidationReport,
        assertions: list[dict[str, object]],
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_COMPLETED,
            {
                **self._event_payload(contract=contract, phase=phase),
                "validation_report_id": report.id,
                "verdict": report.verdict.value,
                "assertions": assertions,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_timeout(
        self,
        *,
        task_id: str,
        loop_id: int,
        contract: ValidationContract,
        phase: MissionPhase,
        assertion: ValidationAssertion,
        result: UserTestingPredicateResult,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_USER_TESTING_TIMEOUT,
            {
                **self._event_payload(contract=contract, phase=phase),
                "assertion_id": assertion.assertion_id,
                "check_type": assertion.check_type,
                "argv": list(result.argv),
                "stdout": result.stdout[:_MAX_EVENT_OUTPUT_CHARS],
                "stderr": result.stderr[:_MAX_EVENT_OUTPUT_CHARS],
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    @staticmethod
    def _event_payload(
        *,
        contract: ValidationContract,
        phase: MissionPhase,
    ) -> dict[str, object]:
        return {"mission_id": contract.mission_id, "phase_id": phase.phase_id}

    @staticmethod
    def _decide_verdict(
        assertions: list[ValidationAssertion],
        missing_evidence: list[str],
    ) -> ValidationVerdict:
        if missing_evidence:
            return ValidationVerdict.FAIL
        if all(assertion.status == "passed" for assertion in assertions):
            return ValidationVerdict.PASS
        if any(assertion.status == "failed" for assertion in assertions):
            return ValidationVerdict.FAIL
        if any(assertion.status == "passed" for assertion in assertions):
            return ValidationVerdict.PARTIAL
        return ValidationVerdict.PARTIAL

    def _missing_prior_evidence_ids(
        self,
        task_id: str,
        assertion: ValidationAssertion,
    ) -> list[str]:
        raw_ids = assertion.params.get("required_evidence_ids")
        if raw_ids is None:
            return []
        if not isinstance(raw_ids, list) or not all(
            isinstance(item, str) for item in raw_ids
        ):
            return ["malformed_required_evidence_ids"]
        missing: list[str] = []
        for evidence_id in raw_ids:
            if self.repo.count_evidence_by_type(
                task_id,
                [evidence_id],
                "any",
            ) == 0:
                missing.append(evidence_id)
        return missing

    @staticmethod
    def _recommended_next_actions(
        assertions: list[ValidationAssertion],
    ) -> list[str]:
        return [
            f"Fix user-testing assertion {assertion.assertion_id}: {assertion.title}"
            for assertion in assertions
            if assertion.status == "failed"
        ][:3]

    def _has_user_testing_event(
        self,
        task_id: str,
        loop_id: int,
        event_type: str,
    ) -> bool:
        return bool(
            self.repo.list_events(
                task_id,
                since_loop=loop_id,
                until_loop=loop_id,
                event_types=[event_type],
            )
        )


__all__ = ["UserTestingValidator"]
