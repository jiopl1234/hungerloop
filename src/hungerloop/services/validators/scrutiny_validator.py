"""Deterministic scrutiny validator for HungerLoop v0.6."""
from __future__ import annotations

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.handoff import HandoffProcessingResult
from hungerloop.models.mission import MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationAssertionStatus,
    ValidationContract,
)
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.workspace_manager import WorkspaceManager

_SCRUTINY_COMMANDS: list[tuple[str, list[str], str]] = [
    ("scrutiny_test", ["python", "-m", "pytest", "-q"], "pytest"),
    ("scrutiny_lint", ["ruff", "check", "src", "tests"], "ruff"),
    ("scrutiny_typecheck", ["mypy", "--strict", "src/"], "mypy"),
]
_MAX_EVENT_OUTPUT_CHARS = 5000


class ScrutinyValidator:
    """Run deterministic repo scrutiny at validating boundaries."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        sandbox_runner: SandboxRunner,
        workspace_manager: WorkspaceManager,
        handoff_processor: HandoffProcessor | None = None,
    ) -> None:
        self.repo = repo
        self.sandbox_runner = sandbox_runner
        self.workspace_manager = workspace_manager
        self.handoff_processor = handoff_processor or HandoffProcessor(repo)

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
        """Run pytest, ruff, and mypy through the sandbox and record assertions."""
        candidate_root = resolve_workspace_path(
            self.workspace_manager.root,
            f"tasks/{task_id}/candidates/loop_{loop_id:03d}/files",
        )
        candidate_root.mkdir(parents=True, exist_ok=True)

        assertions: list[ValidationAssertion] = []
        evidence_ids: list[str] = []
        failed_items: list[HandoffItem] = []
        event_assertions: list[dict[str, object]] = []
        emit_lifecycle_events = not self._has_scrutiny_event(
            task_id,
            loop_id,
            EventType.VALIDATION_SCRUTINY_STARTED.value,
        )

        if emit_lifecycle_events:
            self.repo.append_event(
                EventType.VALIDATION_SCRUTINY_STARTED,
                self._event_payload(contract=contract, phase=phase),
                task_id=task_id,
                loop_id=loop_id,
            )

        for check_type, argv, label in _SCRUTINY_COMMANDS:
            timeout = budget.scrutiny_timeout_seconds
            result = await self.sandbox_runner.run_argv(
                task_id=task_id,
                loop_id=loop_id,
                argv=list(argv),
                cwd=candidate_root,
                timeout=timeout,
                evidence_label=f"{check_type}:{label}",
            )
            if result.evidence_id is not None:
                evidence_ids.append(result.evidence_id)

            status = self._status_for_result(
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )
            assertion = self._persist_assertion(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                phase=phase,
                check_type=check_type,
                label=label,
                argv=argv,
                timeout=timeout,
                status=status,
                evidence_id=result.evidence_id,
            )
            assertions.append(assertion)
            event_assertions.append(
                {
                    "assertion_id": assertion.assertion_id,
                    "check_type": assertion.check_type,
                    "status": assertion.status,
                    "evidence_ids": list(assertion.evidence_ids),
                }
            )

            if result.timed_out:
                self._emit_timeout_event(
                    task_id=task_id,
                    loop_id=loop_id,
                    contract=contract,
                    phase=phase,
                    argv=argv,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            elif status == "failed":
                failed_items.append(
                    HandoffItem(
                        item_type="discovered_issue",
                        summary=f"{check_type} failed",
                        detail=self._failure_detail(
                            label=label,
                            exit_code=result.exit_code,
                            stdout=result.stdout,
                            stderr=result.stderr,
                        ),
                    )
                )

        if failed_items:
            self.handoff_processor.process_handoffs(
                task_id,
                loop_id,
                [
                    WorkerHandoff(
                        agent_id="scrutiny_validator",
                        task_id=task_id,
                        loop_id=loop_id,
                        summary="Scrutiny validation produced failed assertions.",
                        handoff_items=failed_items,
                    )
                ],
                mission=None,
                budget=budget,
            )

        verdict = self._decide_verdict(assertions)
        report = ValidationReport(
            id=f"VAL-scrutiny-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            candidate_state_id=candidate.id,
            baseline_state_id=None,
            verdict=verdict,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            recommended_next_actions=self._recommended_next_actions(task_id),
            has_real_progress=any(assertion.status == "passed" for assertion in assertions),
        )
        if emit_lifecycle_events:
            self.repo.append_event(
                EventType.VALIDATION_SCRUTINY_COMPLETED,
                {
                    **self._event_payload(contract=contract, phase=phase),
                    "validation_report_id": report.id,
                    "verdict": report.verdict.value,
                    "assertions": event_assertions,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        return report

    def _persist_assertion(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        phase: MissionPhase,
        check_type: str,
        label: str,
        argv: list[str],
        timeout: int,
        status: ValidationAssertionStatus,
        evidence_id: str | None,
    ) -> ValidationAssertion:
        evidence_ids = [evidence_id] if evidence_id is not None else []
        assertion = ValidationAssertion(
            assertion_id=self._assertion_id(task_id, loop_id, check_type),
            phase_id=phase.phase_id,
            title=f"Scrutiny {label}",
            description=f"Run {label} scrutiny for candidate {candidate.id}.",
            check_type=check_type,
            params={"argv": list(argv), "timeout": timeout},
            evidence_requirements=["sandbox_run"],
        )
        self.repo.save_validation_assertion(assertion)
        self.repo.update_assertion_status(
            assertion.assertion_id,
            status,
            validated_at_loop=loop_id,
            evidence_ids=evidence_ids,
        )
        return assertion.model_copy(
            update={
                "status": status,
                "validated_at_loop": loop_id,
                "evidence_ids": evidence_ids,
            }
        )

    def _emit_timeout_event(
        self,
        *,
        task_id: str,
        loop_id: int,
        contract: ValidationContract,
        phase: MissionPhase,
        argv: list[str],
        stdout: str,
        stderr: str,
    ) -> None:
        self.repo.append_event(
            EventType.VALIDATION_SCRUTINY_TIMEOUT,
            {
                **self._event_payload(contract=contract, phase=phase),
                "argv": list(argv),
                "stdout": stdout[:_MAX_EVENT_OUTPUT_CHARS],
                "stderr": stderr[:_MAX_EVENT_OUTPUT_CHARS],
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
    def _status_for_result(
        *,
        exit_code: int,
        timed_out: bool,
    ) -> ValidationAssertionStatus:
        if timed_out:
            return "blocked"
        if exit_code == 0:
            return "passed"
        return "failed"

    @staticmethod
    def _failure_detail(
        *,
        label: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> str:
        return stderr.strip() or stdout.strip() or f"{label} exited with code {exit_code}"

    @staticmethod
    def _assertion_id(task_id: str, loop_id: int, check_type: str) -> str:
        return f"SCRUTINY-{task_id}-{loop_id}-{check_type}"

    @staticmethod
    def _decide_verdict(assertions: list[ValidationAssertion]) -> ValidationVerdict:
        if assertions and all(assertion.status == "passed" for assertion in assertions):
            return ValidationVerdict.PASS
        return ValidationVerdict.FAIL

    def _recommended_next_actions(self, task_id: str) -> list[str]:
        result: HandoffProcessingResult | None = (
            self.repo.get_latest_handoff_processing_result(task_id)
        )
        if result is None or not result.prior_handoff_summary:
            return []
        return [result.prior_handoff_summary]

    def _has_scrutiny_event(
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
