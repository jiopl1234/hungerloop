"""Deterministic lifecycle management for synthesized acceptance checks."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus, ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.refinement_compiler import (
    RefinementCompiler,
    _check_dedup_key,
)
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.workspace_manager import WorkspaceManager


@dataclass(frozen=True)
class BaselineValidationResult:
    attempted_item_ids: list[str]
    auto_satisfied_item_ids: list[str]
    failed_item_ids: list[str]
    rejected_fixture_item_ids: list[str]


@dataclass(frozen=True)
class ConflictResolutionResult:
    item_id: str
    signature: str
    occurrence_count: int
    quarantined: bool


class SynthesizedCheckLifecycle:
    """Validate, accept, and quarantine compiler-owned ``H-SYN`` items."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        validation_gate: ValidationGate,
        workspace_manager: WorkspaceManager,
        hunger_update: HungerUpdateService,
        refinement_compiler: RefinementCompiler,
    ) -> None:
        self.repo = repo
        self.validation_gate = validation_gate
        self.workspace_manager = workspace_manager
        self.hunger_update = hunger_update
        self.refinement_compiler = refinement_compiler

    @classmethod
    def has_pending_baseline(cls, ledger: HungerLedger) -> bool:
        """Return whether *ledger* contains actionable pending synthesized checks."""
        return bool(cls._pending_items_from_ledger(ledger))

    async def validate_pending_baseline(
        self,
        *,
        task_id: str,
        loop_id: int,
        workspace_root: Path | None = None,
    ) -> BaselineValidationResult:
        """Validate pending synthesized checks directly against ``best/files``."""
        best = self.repo.get_best_state(task_id)
        if best is None:
            return BaselineValidationResult([], [], [], [])

        pending = self._pending_items(task_id)
        if not pending:
            return BaselineValidationResult([], [], [], [])

        best_root = self.workspace_manager.best_files_dir(task_id)
        validation_root = workspace_root or best_root
        if not validation_root.exists():
            return BaselineValidationResult([], [], [], [])

        candidate = CandidateState(
            id=best.state_id,
            task_id=task_id,
            loop_id=loop_id,
            summary=best.summary,
            artifact_ids=list(best.artifact_ids),
            evidence_ids=list(best.evidence_ids),
            workspace_ref="best",
        )

        ready: list[HungerItem] = []
        rejected_fixture_ids: list[str] = []
        for item in pending:
            fixture_reason = await self._fixture_rejection_reason(
                item=item,
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                best_root=validation_root,
            )
            if fixture_reason is None:
                ready.append(item)
                continue
            rejected_fixture_ids.append(item.id)
            self.refinement_compiler.resolve_synthesis_item(
                task_id=task_id,
                item_id=item.id,
                resolution_kind="invalid_synthesis",
                resolution_reason=fixture_reason,
                loop_id=loop_id,
            )
            check = item.acceptance_checks[0] if item.acceptance_checks else None
            dedup_key = _check_dedup_key(check) if check is not None else None
            self.repo.append_event(
                EventType.SYNTH_CHECK_FIXTURE_REJECTED,
                {
                    "item_id": item.id,
                    "reason": fixture_reason,
                    "dedup_key": dedup_key,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
            if dedup_key is not None:
                self.repo.append_event(
                    EventType.SYNTH_CHECK_REJECTED,
                    {
                        "reason": fixture_reason,
                        "dedup_keys": [dedup_key],
                        "rejected_proposals": [
                            {
                                "reason": fixture_reason,
                                "dedup_key": dedup_key,
                                "description": item.title,
                            }
                        ],
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )

        if not ready:
            return BaselineValidationResult(
                [item.id for item in pending],
                [],
                [],
                rejected_fixture_ids,
            )

        item_ids = [item.id for item in ready]
        with tempfile.TemporaryDirectory(
            prefix=".hungerloop-synthesis-baseline-",
            dir=validation_root.parent,
        ) as temp_dir:
            baseline_root = Path(temp_dir) / "files"
            shutil.copytree(validation_root, baseline_root)
            report = await self.validation_gate.validate(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                target_hunger_item_ids=item_ids,
                workspace_root=baseline_root,
                require_evidence=False,
                report_id=self._report_id(task_id, loop_id, item_ids),
            )
        self.repo.save_validation_report(report)

        accepted = (
            report.verdict in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}
            and not report.regressed_check_keys
        )
        auto_satisfied = (
            list(report.satisfied_hunger_item_ids) if accepted else []
        )
        if accepted:
            self.hunger_update.apply_validation(task_id, report)
            self._record_baseline_acceptance(task_id, loop_id, report)

        for item in ready:
            self.refinement_compiler.complete_synthesis_baseline(
                task_id=task_id,
                item_id=item.id,
                loop_id=loop_id,
            )
            passed = item.id in auto_satisfied
            self.repo.append_event(
                EventType.SYNTH_CHECK_BASELINE_VALIDATED,
                {
                    "item_id": item.id,
                    "passed": passed,
                    "validation_report_id": report.id,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
            if passed:
                self.repo.append_event(
                    EventType.SYNTH_CHECK_AUTO_SATISFIED,
                    {
                        "item_id": item.id,
                        "validation_report_id": report.id,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )

        failed = [item_id for item_id in item_ids if item_id not in auto_satisfied]
        return BaselineValidationResult(
            [item.id for item in pending],
            auto_satisfied,
            failed,
            rejected_fixture_ids,
        )

    def resolve_conflicts(
        self,
        *,
        task_id: str,
        loop_id: int,
        validation: ValidationReport,
        attempted_hunger_item_ids: list[str],
        candidate_committed: bool,
        exempted_check_keys: set[str],
        threshold: int,
    ) -> list[ConflictResolutionResult]:
        """Count rejected H-SYN/accepted-check conflicts and quarantine repeats."""
        if candidate_committed:
            return []
        regressed = sorted(
            set(validation.regressed_check_keys) - exempted_check_keys
        )
        if not regressed:
            return []

        attempted = set(attempted_hunger_item_ids)
        ledger = self.repo.get_hunger_ledger(task_id)
        item_by_id = {item.id: item for item in ledger.items}
        passed_by_item: dict[str, list[bool]] = {}
        for result in validation.check_results:
            if result.hunger_item_id in attempted:
                passed_by_item.setdefault(result.hunger_item_id, []).append(result.passed)

        passing_synthesized_ids = [
            item_id
            for item_id in sorted(attempted)
            if (item := item_by_id.get(item_id)) is not None
            and self._is_synthesized(item)
            and (item_results := passed_by_item.get(item_id, []))
            and all(item_results)
        ]
        if len(attempted) != 1:
            for item_id in passing_synthesized_ids:
                self.repo.append_event(
                    EventType.SYNTH_CHECK_CONFLICT_DETECTED,
                    {
                        "item_id": item_id,
                        "attribution_status": "ambiguous_multiple_attempted_items",
                        "attempted_hunger_item_ids": sorted(attempted),
                        "signature": None,
                        "occurrence_count": 0,
                        "conflicting_check_keys": regressed,
                        "quarantined": False,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
            return []

        outcomes: list[ConflictResolutionResult] = []
        for item_id in passing_synthesized_ids:
            signature = self._conflict_signature(item_id, regressed)
            count, quarantined = self.refinement_compiler.record_synthesis_conflict(
                task_id=task_id,
                item_id=item_id,
                signature=signature,
                conflicting_check_keys=regressed,
                threshold=threshold,
                loop_id=loop_id,
            )
            self.repo.append_event(
                EventType.SYNTH_CHECK_CONFLICT_DETECTED,
                {
                    "item_id": item_id,
                    "signature": signature,
                    "occurrence_count": count,
                    "conflicting_check_keys": regressed,
                    "quarantined": quarantined,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
            if quarantined:
                self.repo.append_event(
                    EventType.SYNTH_CHECK_QUARANTINED,
                    {
                        "item_id": item_id,
                        "signature": signature,
                        "conflicting_check_keys": regressed,
                        "resolution_kind": "invalid_synthesis",
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
            outcomes.append(
                ConflictResolutionResult(item_id, signature, count, quarantined)
            )
        return outcomes

    def _pending_items(self, task_id: str) -> list[HungerItem]:
        return self._pending_items_from_ledger(
            self.repo.get_hunger_ledger(task_id)
        )

    @classmethod
    def _pending_items_from_ledger(cls, ledger: HungerLedger) -> list[HungerItem]:
        return [
            item
            for item in ledger.items
            if cls._is_synthesized(item)
            and item.synthesis_baseline_pending
            and item.status in {HungerItemStatus.OPEN, HungerItemStatus.WORKING}
            and item.gap_score > 0
        ]

    async def _fixture_rejection_reason(
        self,
        *,
        item: HungerItem,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        best_root: Path,
    ) -> str | None:
        if item.synthesis_fixture_argv is None:
            return None
        fixture = AcceptanceCheck(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": list(item.synthesis_fixture_argv), "timeout": 30},
            description=f"fixture for {item.id}",
        )
        outcomes: list[bool] = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(
                prefix=".hungerloop-synthesis-fixture-",
                dir=best_root.parent,
            ) as temp_dir:
                fixture_root = Path(temp_dir) / "files"
                shutil.copytree(best_root, fixture_root)
                passed, _, _ = await self.validation_gate.runner.run(
                    check=fixture,
                    task_id=task_id,
                    loop_id=loop_id,
                    candidate=candidate,
                    workspace_root=fixture_root,
                )
            outcomes.append(passed)
        if outcomes[0] != outcomes[1]:
            return "fixture_nondeterministic"
        if not outcomes[0]:
            return "fixture_setup_failed"
        return None

    def _record_baseline_acceptance(
        self,
        task_id: str,
        loop_id: int,
        report: ValidationReport,
    ) -> None:
        best = self.repo.get_best_state(task_id)
        if best is None:
            return
        evidence_by_key = {
            result.check_key: result.evidence_id for result in report.check_results
        }
        with self.repo.transaction():
            self.repo.save_best_state(
                best.model_copy(
                    update={
                        "accepted_check_keys": list(
                            dict.fromkeys(
                                [*best.accepted_check_keys, *report.newly_passed_check_keys]
                            )
                        ),
                        "evidence_ids": list(
                            dict.fromkeys([*best.evidence_ids, *report.evidence_ids])
                        ),
                        "validation_id": report.id,
                    }
                )
            )
            for check_key in report.newly_passed_check_keys:
                item_id, index = check_key.split(":", 1)
                self.repo.save_accepted_check(
                    task_id=task_id,
                    check_key=check_key,
                    hunger_item_id=item_id,
                    check_index=int(index),
                    accepted_at_loop=loop_id,
                    validation_id=report.id,
                    evidence_id=evidence_by_key.get(check_key),
                )

    @staticmethod
    def _is_synthesized(item: HungerItem) -> bool:
        return item.generated_by == "synthesizer"

    @staticmethod
    def _report_id(task_id: str, loop_id: int, item_ids: list[str]) -> str:
        digest = hashlib.sha256("\0".join(item_ids).encode()).hexdigest()[:12]
        return f"VAL-SYN-BASE-{task_id}-{loop_id}-{digest}"

    @staticmethod
    def _conflict_signature(item_id: str, regressed: list[str]) -> str:
        payload = "\0".join([item_id, *regressed])
        return hashlib.sha256(payload.encode()).hexdigest()[:24]
