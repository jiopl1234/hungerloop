"""Commit decision logic for HungerLoop v0.4.1.

:class:`CommitManager` enforces invariant I-3: candidates are promoted to
``best/`` only when they demonstrate check-level progress (``newly_passed_check_keys``
non-empty) with no regressions. Score-based commits are explicitly rejected.

The manager delegates workspace promotion/rejection to :class:`WorkspaceManager`
and persists state transitions via the repository protocol (Task 14).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.events import EventType
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.mission_state_updater import MissionStateUpdater
from hungerloop.services.validation_gate import make_check_key
from hungerloop.services.validation_pipeline import ValidationPipelineResult
from hungerloop.services.workspace_manager import WorkspaceManager


class CommitDecision(TypedDict):
    """Result of a commit/reject decision."""
    committed: bool
    reason: str
    verdict: ValidationVerdict


class _MissionStateUpdaterProtocol(Protocol):
    """Narrow protocol for commit-tail mission artifact regeneration."""

    def regenerate(
        self,
        task_id: str,
        *,
        best_workspace_root: Path,
    ) -> object: ...


class CommitManager:
    """Decide whether to promote or reject a candidate based on validation."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        workspace_manager: WorkspaceManager,
        mission_state_updater: _MissionStateUpdaterProtocol | None = None,
    ) -> None:
        self.repo = repo
        self.workspace_manager = workspace_manager
        self.mission_state_updater = mission_state_updater or MissionStateUpdater(repo)

    def apply(
        self,
        candidate: CandidateState,
        validation: ValidationReport | ValidationPipelineResult,
        *,
        completed_feature_ids: list[str] | None = None,
    ) -> CommitDecision:
        """Apply validation: promote if I-3 conditions hold, else reject.

        Args:
            candidate: The candidate state.
            validation: Either the legacy deterministic validation report or the
                v0.6 validation pipeline result. Pipeline commits are still gated
                only by the deterministic report to preserve I-3.

        Returns:
            A decision with ``committed: bool`` and ``reason: str``.

        Note:
            v0.5a (ADR-001): repository writes execute inside
            ``repo.transaction()``. v0.6 M5 also invokes mission artifact
            regeneration before that transaction commits so failed mirror
            regeneration rolls back SQLite commit state.
        """
        report = self._deterministic_report(validation)
        if self._can_commit(report):
            best = BestState(
                task_id=candidate.task_id,
                state_id=candidate.id,
                summary=candidate.summary,
                score=0.0,  # I-3: score is not a commit signal; preserved for schema only
                artifact_ids=candidate.artifact_ids,
                evidence_ids=report.evidence_ids,
                validation_id=report.id,
                updated_at_loop=candidate.loop_id,
                accepted_check_keys=report.currently_passed_check_keys,
                workspace_ref="best",
            )

            # Capture already-accepted keys before this commit so we can
            # avoid emitting duplicate DISCOVERY_CREDIT events for replayed
            # or re-mentioned keys (VAL-DISC-008).
            pre_accepted = {
                str(row["check_key"])
                for row in self.repo.iter_accepted_checks(candidate.task_id)
            }

            regeneration_error: Exception | None = None
            try:
                with self.repo.transaction():
                    self.workspace_manager.promote_candidate_to_best(
                        task_id=candidate.task_id,
                        loop_id=candidate.loop_id,
                    )
                    self.repo.save_best_state(best)
                    self.repo.mark_candidate_committed(candidate.id)
                    # §28.9 / M9: per-check accepted record; only newly-passed
                    # rows are inserted (previously-passed rows already exist).
                    evidence_by_key = self._evidence_by_check_key(report)
                    for check_key in report.newly_passed_check_keys:
                        item_id, idx_str = check_key.split(":", 1)
                        self.repo.save_accepted_check(
                            task_id=candidate.task_id,
                            check_key=check_key,
                            hunger_item_id=item_id,
                            check_index=int(idx_str),
                            accepted_at_loop=candidate.loop_id,
                            validation_id=report.id,
                            evidence_id=evidence_by_key.get(check_key),
                        )
                    # v0.7: emit DISCOVERY_CREDIT for worker-generated checks
                    # that newly pass.  Credit is transactionally tied to the
                    # successful commit so rollback/late-failure paths emit
                    # nothing (VAL-DISC-008 / VAL-DISC-009).
                    self._emit_discovery_credits(
                        task_id=candidate.task_id,
                        loop_id=candidate.loop_id,
                        newly_passed=report.newly_passed_check_keys,
                        pre_accepted=pre_accepted,
                    )
                    for feature_id in dict.fromkeys(completed_feature_ids or []):
                        self.repo.update_feature_status(feature_id, "done")
                    if self.repo.get_mission(candidate.task_id) is not None:
                        try:
                            self.mission_state_updater.regenerate(
                                candidate.task_id,
                                best_workspace_root=self.workspace_manager.best_files_dir(
                                    candidate.task_id
                                ),
                            )
                        except Exception as exc:
                            regeneration_error = exc
                            raise
            except Exception as exc:
                if regeneration_error is None:
                    raise
                self.workspace_manager.reject_candidate(
                    task_id=candidate.task_id,
                    loop_id=candidate.loop_id,
                )
                with self.repo.transaction():
                    self.repo.mark_candidate_rejected(candidate.id)
                    self.repo.append_event(
                        EventType.MISSION_STATE_REGENERATION_FAILED,
                        {
                            "candidate_state_id": candidate.id,
                            "validation_report_id": report.id,
                            "loop_id": candidate.loop_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        task_id=candidate.task_id,
                        loop_id=candidate.loop_id,
                    )
                return {
                    "committed": False,
                    "reason": "mission_state_regeneration_failed",
                    "verdict": ValidationVerdict.FAIL,
                }
            return {
                "committed": True,
                "reason": "validation_passed_with_check_progress",
                "verdict": report.verdict,
            }

        self.workspace_manager.reject_candidate(
            task_id=candidate.task_id,
            loop_id=candidate.loop_id,
        )
        with self.repo.transaction():
            self.repo.mark_candidate_rejected(candidate.id)
            self.repo.add_failure_from_validation(report)

        return {
            "committed": False,
            "reason": self._reject_reason(report),
            "verdict": report.verdict,
        }

    @staticmethod
    def _deterministic_report(
        validation: ValidationReport | ValidationPipelineResult,
    ) -> ValidationReport:
        if isinstance(validation, ValidationPipelineResult):
            return validation.deterministic_report
        return validation

    @staticmethod
    def _evidence_by_check_key(report: ValidationReport) -> dict[str, str | None]:
        """Map ``check_key`` to the first evidence_id from CheckResult, if any."""
        return {
            make_check_key(r.hunger_item_id, r.check_index): r.evidence_id
            for r in report.check_results
        }

    def _can_commit(self, report: ValidationReport) -> bool:
        """Check if a report satisfies I-3 commit conditions."""
        if report.verdict not in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}:
            return False
        if not report.newly_passed_check_keys:
            return False
        if report.regressed_check_keys:
            return False
        if report.missing_evidence:
            return False
        return True

    def _reject_reason(self, report: ValidationReport) -> str:
        """Determine the rejection reason from a report."""
        if report.verdict == ValidationVerdict.FAIL:
            return "verdict_fail"
        if not report.newly_passed_check_keys:
            return "no_new_check_progress"
        if report.regressed_check_keys:
            return "regressed_checks_detected"
        if report.missing_evidence:
            return "missing_evidence"
        return "unknown"

    def _emit_discovery_credits(
        self,
        *,
        task_id: str,
        loop_id: int,
        newly_passed: list[str],
        pre_accepted: set[str],
    ) -> None:
        """Emit DISCOVERY_CREDIT events for worker-generated checks.

        For each unique first-time newly-passed check key owned by a hunger
        item with ``generated_by`` starting ``worker:``, emit exactly one
        ``DISCOVERY_CREDIT`` event.  Keys already accepted before this
        commit (replays) and non-worker items are silently skipped.

        This method is called inside the commit transaction so credit
        events are atomically tied to the successful commit (VAL-DISC-008,
        VAL-DISC-009).
        """
        ledger = self.repo.get_hunger_ledger(task_id)
        items_by_id: dict[str, object] = {item.id: item for item in ledger.items}
        seen: set[str] = set()
        for check_key in newly_passed:
            if check_key in seen:
                continue
            seen.add(check_key)
            # Skip keys that were already accepted before this commit.
            if check_key in pre_accepted:
                continue
            item_id = check_key.split(":", 1)[0]
            item = items_by_id.get(item_id)
            if item is None:
                continue
            generated_by = getattr(item, "generated_by", None)
            if not isinstance(generated_by, str) or not generated_by.startswith("worker:"):
                continue
            proposer = generated_by[len("worker:"):]
            self.repo.append_event(
                EventType.DISCOVERY_CREDIT,
                {
                    "proposer": proposer,
                    "check_key": check_key,
                    "loop_id": loop_id,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
