"""Commit decision logic for HungerLoop v0.4.1.

:class:`CommitManager` enforces invariant I-3: candidates are promoted to
``best/`` only when they demonstrate check-level progress (``newly_passed_check_keys``
non-empty) with no regressions. Score-based commits are explicitly rejected.

The manager delegates workspace promotion/rejection to :class:`WorkspaceManager`
and persists state transitions via the repository protocol (Task 14).
"""

from __future__ import annotations

from typing import TypedDict

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.validation_gate import make_check_key
from hungerloop.services.workspace_manager import WorkspaceManager


class CommitDecision(TypedDict):
    """Result of a commit/reject decision."""
    committed: bool
    reason: str


class CommitManager:
    """Decide whether to promote or reject a candidate based on validation."""

    def __init__(
        self, repo: RepositoryProtocol, workspace_manager: WorkspaceManager
    ) -> None:
        self.repo = repo
        self.workspace_manager = workspace_manager

    def apply(self, candidate: CandidateState, report: ValidationReport) -> CommitDecision:
        """Apply a validation report: promote if I-3 conditions hold, else reject.

        Args:
            candidate: The candidate state.
            report: The validation report.

        Returns:
            A decision with ``committed: bool`` and ``reason: str``.

        Note:
            v0.5a (ADR-001): the repository writes that follow promotion or
            rejection execute inside ``repo.transaction()``. Filesystem
            operations remain outside the transaction (they cannot
            participate in SQLite atomicity); recovery on restart is the
            Orchestrator's job.
        """
        if self._can_commit(report):
            self.workspace_manager.promote_candidate_to_best(
                task_id=candidate.task_id,
                loop_id=candidate.loop_id,
            )

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

            with self.repo.transaction():
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
            return {
                "committed": True,
                "reason": "validation_passed_with_check_progress",
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
        }

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
