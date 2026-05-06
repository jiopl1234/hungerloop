"""MemoryManager + promotion predicates (PRD §15 / §19).

The four predicates from §19.2 are implemented as pure helpers so they
can be unit-tested independently:

* ``action_verified`` — at least one ``evidence_id`` is present in
  ``best.evidence_ids``.
* ``reusable`` — content is free of task-specific identifiers; uses
  the anchored regex set from PRD §15 / FR-8 (``is_reusable``) so a
  substring like ``"taskid"`` doesn't false-positive on
  ``"task_<uuid>"``.
* ``non_volatile`` — the validated source best-state was the *final*
  best-state of a DONE-stopped task. Mid-task or non-DONE stops never
  flip this True (PRD §19.2 / §15 / FR-7).
* ``traceable`` — ``set(evidence_ids) ⊆ set(best.evidence_ids)``.

:class:`MemoryManager.propose_from_loop` produces one candidate per
``newly_passed_check_key`` in the loop's validation report, saves each via
``repo.save_memory_candidate``, and returns the list for assertions / CLI.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from hungerloop.models.enums import StopReason
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol

# Default lifetime for an emitted candidate (PRD §19.1 + decision §11.4):
# 90 days from creation. Pure data in v0.5c — no auto-job acts on this
# until v0.6's expiry sweep lands.
_CANDIDATE_TTL = timedelta(days=90)

# PRD §15 / FR-8: anchored regex patterns identifying task-specific
# tokens. Anchored, NOT substring matches. Compiled once at module
# import so per-candidate evaluation is sub-millisecond (NFR-3).
TASK_SPECIFIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btask_[0-9a-fA-F-]+\b"),
    re.compile(r"\bloop_\d{3}\b"),
    re.compile(r"^/tmp/", re.MULTILINE),
    re.compile(r"workspace/tasks/[a-zA-Z0-9_-]+"),
    re.compile(r"\bCAND-[a-zA-Z0-9_-]+\b"),
    re.compile(r"\bVAL-[a-zA-Z0-9_-]+\b"),
)


def is_reusable(content: str) -> bool:
    """True iff ``content`` avoids the FR-8 task-specific patterns."""
    return not any(pattern.search(content) for pattern in TASK_SPECIFIC_PATTERNS)


def action_verified(candidate: MemoryCandidate, best_evidence_ids: list[str]) -> bool:
    """True if any candidate evidence_id is also referenced by best (§19.2)."""
    if not candidate.evidence_ids or not best_evidence_ids:
        return False
    return any(eid in best_evidence_ids for eid in candidate.evidence_ids)


def reusable(candidate: MemoryCandidate) -> bool:
    """True if content avoids the FR-8 task-specific patterns (§19.2)."""
    return is_reusable(candidate.content)


def non_volatile(
    candidate: MemoryCandidate, repo: RepositoryProtocol
) -> bool:
    """True iff this candidate's source is the final best-state of a
    DONE-stopped task (PRD §15 / FR-7).

    Mid-task or non-DONE stops never flip this True. Returns False
    when no StopReport exists yet.
    """
    if candidate.source_best_state_id is None:
        return False
    last_report = repo.get_last_stop_report(candidate.task_id)
    if last_report is None:
        return False
    if last_report.stop_reason is not StopReason.DONE:
        return False
    return candidate.source_best_state_id == last_report.final_best_state_id


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

        # PRD §14.4 / FR-5: referenced vs accepted distinction.
        attempted = (
            validation.attempted_check_keys
            or validation.newly_passed_check_keys
        )
        accepted_set = set(validation.newly_passed_check_keys)

        candidates: list[MemoryCandidate] = []
        for check_key in validation.newly_passed_check_keys:
            candidate_id = f"mem-{uuid.uuid4().hex[:8]}"
            candidate = MemoryCandidate(
                candidate_id=candidate_id,
                task_id=task_id,
                content=f"Verified acceptance check {check_key}",
                memory_type="fact",
                evidence_ids=list(validation.evidence_ids),
                referenced_check_keys=list(attempted),
                accepted_check_keys=[
                    key for key in attempted if key in accepted_set
                ],
                source_loop_ids=[loop_id],
                source_candidate_state_id=validation.candidate_state_id,
                source_validation_id=validation.id,
                source_best_state_id=best.state_id if best is not None else None,
                state="proposed",
                expires_at=expires_at,
            )
            candidate.action_verified = action_verified(
                candidate, best_evidence_ids
            )
            candidate.reusable = reusable(candidate)
            candidate.non_volatile = non_volatile(candidate, self.repo)
            candidate.traceable = traceable(candidate, best_evidence_ids)

            self.repo.save_memory_candidate(candidate)
            candidates.append(candidate)

        return candidates
