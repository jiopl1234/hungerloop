"""MemoryManager + promotion predicates (PRD §19).

v0.5c only generates :class:`MemoryCandidate` rows; promotion to long-term
memory is a v0.5d concern. The four predicates from §19.2 are implemented as
pure helpers so they can be unit-tested independently:

* ``action_verified`` — at least one ``evidence_id`` is present in
  ``best.evidence_ids``.
* ``reusable`` — content is free of task-specific identifiers (``task_id``,
  ``candidate_id``, ``loop_id`` token).
* ``non_volatile`` — the validated source best/candidate state has been
  observed in committed state history, rather than comparing the memory
  candidate's own id to a best-state id.
* ``traceable`` — ``set(evidence_ids) ⊆ set(best.evidence_ids)``.

:class:`MemoryManager.propose_from_loop` produces one candidate per
``newly_passed_check_key`` in the loop's validation report, saves each via
``repo.save_memory_candidate``, and returns the list for assertions / CLI.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol

_LOOP_TOKEN = re.compile(r"loop[_\s-]?\d+", re.IGNORECASE)

# Default lifetime for an emitted candidate (PRD §19.1 + decision §11.4):
# 90 days from creation. Pure data in v0.5c — no auto-job acts on this
# until v0.6's expiry sweep lands.
_CANDIDATE_TTL = timedelta(days=90)


def action_verified(candidate: MemoryCandidate, best_evidence_ids: list[str]) -> bool:
    """True if any candidate evidence_id is also referenced by best (§19.2)."""
    if not candidate.evidence_ids or not best_evidence_ids:
        return False
    return any(eid in best_evidence_ids for eid in candidate.evidence_ids)


def reusable(candidate: MemoryCandidate, *, task_id: str) -> bool:
    """True if content avoids task-specific tokens (§19.2)."""
    content = candidate.content
    if task_id and task_id in content:
        return False
    if candidate.candidate_id in content:
        return False
    if _LOOP_TOKEN.search(content):
        return False
    return True


def non_volatile(
    candidate: MemoryCandidate, repo: RepositoryProtocol
) -> bool:
    """True if the source state is referenced by committed state history."""
    source_ids = [
        candidate.source_best_state_id,
        candidate.source_candidate_state_id,
    ]
    return any(
        source_id is not None and repo.count_committed_references(source_id) >= 2
        for source_id in source_ids
    )


def traceable(
    candidate: MemoryCandidate, best_evidence_ids: list[str]
) -> bool:
    """True if every candidate evidence_id is in best.evidence_ids (§19.2)."""
    if not candidate.evidence_ids:
        return False
    best_set = set(best_evidence_ids)
    return all(eid in best_set for eid in candidate.evidence_ids)


class MemoryManager:
    """Generate :class:`MemoryCandidate` rows from validated loops (§19.3)."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def propose_from_loop(
        self,
        task_id: str,
        loop_id: int,
        validation: ValidationReport,
        *,
        now: datetime | None = None,
    ) -> list[MemoryCandidate]:
        """Emit one candidate per newly-passed check.

        Returns an empty list when no check newly passed (PRD §19.3 guard).
        Predicates are evaluated against the current ``best_state`` so the
        candidate's flags are stable at write time even if later commits
        change the picture; CLI ``memory list`` re-reads the persisted row.

        ``now`` is plumbing for tests so the 90-day ``expires_at`` value is
        deterministic; production callers leave it ``None`` to use UTC now.
        """
        if not validation.newly_passed_check_keys:
            return []

        created_at = now or datetime.now(timezone.utc)
        expires_at = created_at + _CANDIDATE_TTL

        best = self.repo.get_best_state(task_id)
        best_evidence_ids = list(best.evidence_ids) if best is not None else []

        candidates: list[MemoryCandidate] = []
        for check_key in validation.newly_passed_check_keys:
            candidate_id = f"mem-{uuid.uuid4().hex[:8]}"
            candidate = MemoryCandidate(
                candidate_id=candidate_id,
                task_id=task_id,
                content=f"Verified acceptance check {check_key}",
                memory_type="fact",
                evidence_ids=list(validation.evidence_ids),
                referenced_check_keys=[check_key],
                source_loop_ids=[loop_id],
                source_candidate_state_id=validation.candidate_state_id,
                source_validation_id=validation.id,
                source_best_state_id=best.state_id if best is not None else None,
                # v0.5c.0: only "proposed" is emitted. Promotion to
                # "approved"/"rejected"/"expired"/"superseded" lands in
                # v0.6 — it'll be a pure repo write against the row we
                # already persist here.
                state="proposed",
                expires_at=expires_at,
            )
            candidate.action_verified = action_verified(
                candidate, best_evidence_ids
            )
            candidate.reusable = reusable(candidate, task_id=task_id)
            candidate.non_volatile = non_volatile(candidate, self.repo)
            candidate.traceable = traceable(candidate, best_evidence_ids)

            self.repo.save_memory_candidate(candidate)
            candidates.append(candidate)

        return candidates
