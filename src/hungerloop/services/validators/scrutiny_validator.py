"""Deterministic scrutiny validator for HungerLoop v0.6."""
from __future__ import annotations

import shlex
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import EvidenceType, ValidationVerdict
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
_SCRUTINY_COMMAND_KEYS: tuple[tuple[str, str, str], ...] = (
    ("test", "scrutiny_test", "pytest"),
    ("lint", "scrutiny_lint", "ruff"),
    ("typecheck", "scrutiny_typecheck", "mypy"),
)
_WORKSPACE_CHECK_TYPE = "scrutiny_workspace"
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

        if not candidate_root.is_dir():
            report = self._missing_workspace_report(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                contract=contract,
                phase=phase,
                candidate_root=candidate_root,
            )
            if emit_lifecycle_events:
                self.repo.append_event(
                    EventType.VALIDATION_SCRUTINY_COMPLETED,
                    {
                        **self._event_payload(contract=contract, phase=phase),
                        "validation_report_id": report.id,
                        "verdict": report.verdict.value,
                        "assertions": [
                            {
                                "assertion_id": self._assertion_id(
                                    task_id,
                                    loop_id,
                                    _WORKSPACE_CHECK_TYPE,
                                ),
                                "check_type": _WORKSPACE_CHECK_TYPE,
                                "status": "blocked",
                                "evidence_ids": list(report.evidence_ids),
                            }
                        ],
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
            return report

        for check_type, argv, label in self._commands_for_task(task_id):
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
            self._emit_assertion_result(
                task_id=task_id,
                loop_id=loop_id,
                contract=contract,
                phase=phase,
                assertion=assertion,
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

    def _missing_workspace_report(
        self,
        *,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        contract: ValidationContract,
        phase: MissionPhase,
        candidate_root: Path,
    ) -> ValidationReport:
        evidence_id = self.repo.save_evidence(
            task_id=task_id,
            loop_id=loop_id,
            evidence_type=EvidenceType.VALIDATION_CHECK,
            payload={
                **self._event_payload(contract=contract, phase=phase),
                "candidate_state_id": candidate.id,
                "workspace_ref": candidate.workspace_ref,
                "candidate_root": str(candidate_root),
                "check_type": _WORKSPACE_CHECK_TYPE,
                "reason": "candidate_workspace_missing",
                "success": False,
            },
        )
        assertion = self._persist_assertion(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            phase=phase,
            check_type=_WORKSPACE_CHECK_TYPE,
            label="workspace",
            argv=[],
            timeout=0,
            status="blocked",
            evidence_id=evidence_id,
            evidence_requirement="validation_check",
            params={
                "workspace_ref": candidate.workspace_ref,
                "candidate_root": str(candidate_root),
                "reason": "candidate_workspace_missing",
            },
        )
        self.repo.append_event(
            EventType.VALIDATION_SCRUTINY_WORKSPACE_MISSING,
            {
                **self._event_payload(contract=contract, phase=phase),
                "candidate_state_id": candidate.id,
                "workspace_ref": candidate.workspace_ref,
                "candidate_root": str(candidate_root),
                "assertion_id": assertion.assertion_id,
                "evidence_ids": list(assertion.evidence_ids),
                "reason": "candidate_workspace_missing",
            },
            task_id=task_id,
            loop_id=loop_id,
        )
        return ValidationReport(
            id=f"VAL-scrutiny-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            candidate_state_id=candidate.id,
            baseline_state_id=None,
            verdict=ValidationVerdict.FAIL,
            evidence_ids=[evidence_id],
            missing_evidence=[
                f"Candidate workspace does not exist: {candidate.workspace_ref}"
            ],
            recommended_next_actions=[
                "Create or copy the candidate workspace before scrutiny validation."
            ],
            has_real_progress=False,
        )

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
        evidence_requirement: str = "sandbox_run",
        params: dict[str, object] | None = None,
    ) -> ValidationAssertion:
        evidence_ids = [evidence_id] if evidence_id is not None else []
        assertion = ValidationAssertion(
            assertion_id=self._assertion_id(task_id, loop_id, check_type),
            phase_id=phase.phase_id,
            title=f"Scrutiny {label}",
            description=f"Run {label} scrutiny for candidate {candidate.id}.",
            check_type=check_type,
            params=(
                params
                if params is not None
                else {"argv": list(argv), "timeout": timeout}
            ),
            evidence_requirements=[evidence_requirement],
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

    def _emit_assertion_result(
        self,
        *,
        task_id: str,
        loop_id: int,
        contract: ValidationContract,
        phase: MissionPhase,
        assertion: ValidationAssertion,
    ) -> None:
        event_type = (
            EventType.VALIDATION_ASSERTION_PASSED
            if assertion.status == "passed"
            else EventType.VALIDATION_ASSERTION_FAILED
        )
        self.repo.append_event(
            event_type,
            {
                **self._event_payload(contract=contract, phase=phase),
                "assertion_id": assertion.assertion_id,
                "check_type": assertion.check_type,
                "status": assertion.status,
                "evidence_ids": list(assertion.evidence_ids),
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
        if all(assertion.status == "passed" for assertion in assertions):
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

    def _commands_for_task(
        self,
        task_id: str,
    ) -> list[tuple[str, list[str], str]]:
        """Resolve scrutiny argv triples for ``task_id``.

        Resolution order:

        - mission has no ``services_manifest['commands']`` mapping ->
          fall back to the hardcoded :data:`_SCRUTINY_COMMANDS`.
        - ``commands`` is a mapping (possibly empty) -> honor exactly
          the keys it specifies. An explicit empty mapping disables
          scrutiny commands entirely; a partial mapping only runs the
          configured stages.
        """
        mission = self.repo.get_mission(task_id)
        manifest = mission.services_manifest if mission is not None else None
        commands = manifest.get("commands") if isinstance(manifest, dict) else None
        if not isinstance(commands, dict):
            return [(key, list(argv), label) for key, argv, label in _SCRUTINY_COMMANDS]

        resolved: list[tuple[str, list[str], str]] = []
        for command_key, check_type, label in _SCRUTINY_COMMAND_KEYS:
            if command_key not in commands:
                continue
            argv = _coerce_command_argv(commands.get(command_key))
            if argv:
                resolved.append((check_type, argv, label))
        return resolved


def _coerce_command_argv(raw: object) -> list[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    return []
