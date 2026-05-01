"""Commit decision logic for HungerLoop v0.4.1.

:class:`CommitManager` enforces invariant I-3: candidates are promoted to
``best/`` only when they demonstrate check-level progress (``newly_passed_check_keys``
non-empty) with no regressions. Score-based commits are explicitly rejected.

The manager delegates workspace promotion/rejection to :class:`WorkspaceManager`
and persists state transitions via the repository protocol (Task 14).
"""

from __future__ import annotations

from typing import Any, TypedDict

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import ValidationReport
from hungerloop.services.workspace_manager import WorkspaceManager


class CommitDecision(TypedDict):
    """Result of a commit/reject decision."""
    committed: bool
    reason: str


class CommitManager:
    """Decide whether to promote or reject a candidate based on validation."""

    def __init__(self, repo: Any, workspace_manager: WorkspaceManager) -> None:
        # TODO(Task 14): tighten ``repo`` to the Repository protocol once it lands.
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
            Operations are not transactional. If a later step fails after the
            workspace has been promoted/rejected, state may diverge between the
            filesystem and the repository. Task 14's repository will provide
            idempotent calls; recovery on restart is the orchestrator's job.
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

            self.repo.save_best_state(best)
            self.repo.mark_candidate_committed(candidate.id)
            return {
                "committed": True,
                "reason": "validation_passed_with_check_progress",
            }

        self.workspace_manager.reject_candidate(
            task_id=candidate.task_id,
            loop_id=candidate.loop_id,
        )
        self.repo.mark_candidate_rejected(candidate.id)
        self.repo.add_failure_from_validation(report)

        return {
            "committed": False,
            "reason": self._reject_reason(report),
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
