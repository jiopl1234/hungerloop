"""Integrator service for HungerLoop v0.4.1.

:class:`Integrator` combines multiple worker results into a single candidate state.
"""
from __future__ import annotations

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.worker import WorkerResult


class Integrator:
    """Integrate worker results into a candidate state."""

    def integrate(
        self, task_id: str, loop_id: int, results: list[WorkerResult]
    ) -> CandidateState:
        """Combine worker results into a candidate state.

        Args:
            task_id: Task identifier.
            loop_id: Loop iteration.
            results: Worker results to integrate.

        Returns:
            A candidate state aggregating all worker outputs.
        """
        summaries: list[str] = []
        artifact_ids: list[str] = []
        evidence_ids: list[str] = []
        claim_ids: list[str] = []

        for r in results:
            if r.summary:
                summaries.append(r.summary)
            artifact_ids.extend(r.artifact_ids)
            evidence_ids.extend(r.evidence_ids)
            claim_ids.extend(r.claim_ids)

        return CandidateState(
            id=f"CAND-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            summary="\n".join(summaries),
            artifact_ids=list(dict.fromkeys(artifact_ids)),
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            claim_ids=list(dict.fromkeys(claim_ids)),
            proposed_score=0.0,
            workspace_ref=f"candidates/loop_{loop_id:03d}",
        )
