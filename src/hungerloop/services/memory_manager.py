"""MemoryManager + promotion predicates (PRD 15 / 19).

The four predicates from 19.2 are implemented as pure helpers so they
can be unit-tested independently:

* `action_verified` - at least one `evidence_id` is present in
  `best.evidence_ids`.
* `reusable` - content is free of task-specific identifiers; uses
  the anchored regex set from PRD 15 / FR-8 (`is_reusable`) so a
  substring like `"taskid"` doesn't false-positive on
  `"task_<uuid>"`.
* `non_volatile` - the validated source best-state was the *final*
  best-state of a DONE-stopped task. Mid-task or non-DONE stops never
  flip this True (PRD 19.2 / 15 / FR-7).
* `traceable` - `set(evidence_ids) subset set(best.evidence_ids)`.

:class:MemoryManager.propose_from_loop produces one candidate per
`newly_passed_check_key` in the loop's validation report, saves each via
`repo.save_memory_candidate`, and returns the list for assertions / CLI.

v0.7 upgrades candidate content from bookkeeping strings to reusable
check/tool insights and adds predicate-gated `auto_promote` after
DONE stop reports.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from hungerloop.models.enums import EvidenceType, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.memory import MemoryCandidate, PromotedMemory
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.protocol import RepositoryProtocol

# Default lifetime for an emitted candidate (PRD 19.1 + decision 11.4):
# 90 days from creation. Pure data in v0.5c - no auto-job acts on this
# until v0.6's expiry sweep lands.
_CANDIDATE_TTL = timedelta(days=90)

# Maximum character length for the evidence digest embedded in candidate
# content (VAL-MEM-017).
_DIGEST_MAX_CHARS = 200

# Patterns that must never leak into candidate content or evidence digests.
# These extend the task-specific patterns to include secrets and volatile
# identifiers that VAL-MEM-001 / VAL-MEM-017 explicitly prohibit.
_VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btask_[0-9a-fA-F-]+\b"),
    re.compile(r"\bloop_\d{3}\b"),
    re.compile(r"^/tmp/", re.MULTILINE),
    re.compile(r"workspace/tasks/[a-zA-Z0-9_-]+"),
    re.compile(r"\bCAND-[a-zA-Z0-9_-]+\b"),
    re.compile(r"\bVAL-[a-zA-Z0-9_-]+\b"),
    re.compile(r"\bbest/", re.MULTILINE),
    re.compile(r"candidates/loop_\d+", re.MULTILINE),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"(?i)api[_-]?key"),
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9._-]+"),
    re.compile(r"(?i)\.env\b"),
    # Windows absolute paths (e.g. C:\Users\..., D:/data/...)
    re.compile(r"[A-Za-z]:[\\/]"),
    # POSIX absolute paths beyond /tmp (e.g. /home/user, /var/log)
    re.compile(r"(?<!\w)/(?:home|var|usr|opt|etc|root|mnt|media)\b"),
)

# PRD 15 / FR-8: anchored regex patterns identifying task-specific
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
    """True iff `content` avoids the FR-8 task-specific patterns."""
    return not any(pattern.search(content) for pattern in TASK_SPECIFIC_PATTERNS)


def _is_prompt_safe(text: str) -> bool:
    """True iff `text` is free of volatile identifiers and secrets."""
    return not any(pattern.search(text) for pattern in _VOLATILE_PATTERNS)


def action_verified(candidate: MemoryCandidate, best_evidence_ids: list[str]) -> bool:
    """True if any candidate evidence_id is also referenced by best (19.2)."""
    if not candidate.evidence_ids or not best_evidence_ids:
        return False
    return any(eid in best_evidence_ids for eid in candidate.evidence_ids)


def reusable(candidate: MemoryCandidate) -> bool:
    """True if content avoids the FR-8 task-specific patterns (19.2)."""
    return is_reusable(candidate.content)


def non_volatile(
    candidate: MemoryCandidate, repo: RepositoryProtocol
) -> bool:
    """True iff this candidate's source is the final best-state of a
    DONE-stopped task (PRD 15 / FR-7).

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
    """True if every candidate evidence_id is in best.evidence_ids (19.2)."""
    if not candidate.evidence_ids:
        return False
    best_set = set(best_evidence_ids)
    return all(eid in best_set for eid in candidate.evidence_ids)


def _resolve_check_key(
    ledger: HungerLedger, check_key: str
) -> tuple[str, int, str, str] | None:
    """Resolve a `check_key` to `(item_id, check_index, item_title, check_description)`.

    Returns `None` when the key is malformed, the item is missing,
    or the check index is out of range (VAL-MEM-020).
    """
    if ":" not in check_key:
        return None
    parts = check_key.split(":", 1)
    if len(parts) != 2:
        return None
    item_id, idx_str = parts
    try:
        check_index = int(idx_str)
    except ValueError:
        return None
    # Find the exact hunger item by id
    matching_items = [item for item in ledger.items if item.id == item_id]
    if len(matching_items) != 1:
        return None
    item = matching_items[0]
    if check_index < 0 or check_index >= len(item.acceptance_checks):
        return None
    check = item.acceptance_checks[check_index]
    return (item_id, check_index, item.title, check.description)


def _build_evidence_digest(
    repo: RepositoryProtocol,
    task_id: str,
    evidence_ids: list[str],
    loop_id: int,
) -> str | None:
    """Build a deterministic, bounded, prompt-safe evidence digest.

    Uses only successful tool-call evidence for the source loop.
    Returns `None` when no prompt-safe successful evidence is available
    (VAL-MEM-017).
    """
    if not evidence_ids:
        return None
    all_evidence = repo.list_evidence(task_id)
    evidence_map: dict[str, dict[str, object]] = {
        str(e.get("evidence_id", "")): e for e in all_evidence
    }
    digests: list[str] = []
    for eid in evidence_ids:
        ev = evidence_map.get(eid)
        if ev is None:
            continue
        ev_type = str(ev.get("type", ""))
        if ev_type != EvidenceType.TOOL_CALL.value:
            continue
        success = ev.get("success")
        if success is not True and str(success).lower() != "true":
            continue
        tool_name = str(ev.get("tool_name", ""))
        result_summary = str(ev.get("result_summary", ""))
        # Only include prompt-safe text
        combined = f"{tool_name}: {result_summary}"
        if not _is_prompt_safe(combined):
            continue
        digests.append(combined)
    if not digests:
        return None
    # Deterministic ordering by content
    digests.sort()
    full_digest = " | ".join(digests)
    # Bound the digest length
    if len(full_digest) > _DIGEST_MAX_CHARS:
        full_digest = full_digest[:_DIGEST_MAX_CHARS]
    return full_digest


def _build_candidate_content(
    check_key: str,
    item_title: str,
    check_description: str,
    evidence_digest: str | None,
) -> str:
    """Build reusable, task-agnostic candidate content (VAL-MEM-001)."""
    parts: list[str] = [f"Check {check_key}"]
    parts.append(item_title)
    if check_description:
        parts.append(check_description)
    if evidence_digest:
        parts.append(f"Evidence: {evidence_digest}")
    content = " - ".join(parts)
    # Final safety check: ensure no volatile content leaked through
    if not _is_prompt_safe(content):
        # Fallback: strip evidence digest if it caused the issue
        parts_no_digest = [f"Check {check_key}", item_title]
        if check_description:
            parts_no_digest.append(check_description)
        content = " - ".join(parts_no_digest)
    return content


class MemoryManager:
    """Generate :class:MemoryCandidate rows from validated loops (19.3).

    v0.7: Also provides predicate-gated `auto_promote` after DONE stop
    reports.
    """

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

        Returns an empty list when no check newly passed (PRD 19.3 guard).
        Each `newly_passed_check_key` is resolved back to the exact hunger
        item and acceptance-check index before building content (VAL-MEM-001).
        Unresolvable keys (missing, ambiguous, out-of-range) are skipped with
        a stable non-secret reason rather than generating fallback content
        (VAL-MEM-020).

        Predicates are evaluated against the current `best_state` so the
        candidate's flags are stable at write time even if later commits
        change the picture; CLI `memory list` re-reads the persisted row.

        `now` is plumbing for tests so the 90-day `expires_at` value is
        deterministic; production callers leave it `None` to use UTC now.
        """
        if not validation.newly_passed_check_keys:
            return []

        created_at = now or datetime.now(timezone.utc)
        expires_at = created_at + _CANDIDATE_TTL

        best = self.repo.get_best_state(task_id)
        best_evidence_ids = list(best.evidence_ids) if best is not None else []

        # Resolve the hunger ledger so we can map check keys to items.
        ledger = self.repo.get_hunger_ledger(task_id)

        # PRD 14.4 / FR-5: referenced vs accepted distinction.
        attempted = (
            validation.attempted_check_keys
            or validation.newly_passed_check_keys
        )
        accepted_set = set(validation.newly_passed_check_keys)

        # Build evidence digest once for this loop (VAL-MEM-017).
        evidence_digest = _build_evidence_digest(
            self.repo,
            task_id,
            list(validation.evidence_ids),
            loop_id,
        )

        # Track seen keys so duplicates in the report produce only one
        # candidate (VAL-MEM-020).
        seen_keys: set[str] = set()
        candidates: list[MemoryCandidate] = []
        for check_key in validation.newly_passed_check_keys:
            if check_key in seen_keys:
                continue
            seen_keys.add(check_key)

            # Resolve the check key to item + check (VAL-MEM-001 / VAL-MEM-020).
            resolved = _resolve_check_key(ledger, check_key)
            if resolved is None:
                # Unresolvable key: skip without fallback content.
                continue
            _item_id, _check_index, item_title, check_description = resolved

            content = _build_candidate_content(
                check_key,
                item_title,
                check_description,
                evidence_digest,
            )

            candidate_id = f"mem-{uuid.uuid4().hex[:8]}"
            candidate = MemoryCandidate(
                candidate_id=candidate_id,
                task_id=task_id,
                content=content,
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

    def auto_promote(
        self, task_id: str
    ) -> list[PromotedMemory]:
        """Promote eligible memory candidates after a DONE stop report.

        v0.7 (VAL-MEM-003 through VAL-MEM-005, VAL-MEM-007, VAL-MEM-018,
        VAL-MEM-021):

        * Respects `memory_auto_promote_enabled` policy flag.
        * Requires DONE stop report to be persisted before promotion.
        * Considers only pending, unreviewed, unexpired candidates.
        * Requires all four predicates to be true: action_verified,
          reusable, non_volatile, traceable.
        * Writes candidate review state, promoted-memory row, and event
          atomically inside a repository transaction.
        * Is idempotent: already-approved candidates are not re-promoted.
        * Skips rejected, superseded, deferred, already-approved, and
          expired candidates.
        """
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.memory_auto_promote_enabled:
            return []

        # DONE stop report must be persisted before auto-promotion
        # (VAL-MEM-007).
        last_report = self.repo.get_last_stop_report(task_id)
        if last_report is None:
            return []
        if last_report.stop_reason is not StopReason.DONE:
            return []

        final_best_state_id = last_report.final_best_state_id

        # Re-evaluate predicates against current final state.
        best = self.repo.get_best_state(task_id)
        best_evidence_ids = list(best.evidence_ids) if best is not None else []
        best_state_id = best.state_id if best is not None else None

        now = datetime.now(timezone.utc).replace(microsecond=0)

        candidates = self.repo.list_memory_candidates(task_id)
        promoted: list[PromotedMemory] = []
        for cand in candidates:
            # Skip ineligible lifecycle states (VAL-MEM-021).
            if cand.state != "proposed":
                continue
            # Skip expired candidates (by lifecycle state or by time).
            if cand.expires_at is not None and cand.expires_at <= now:
                continue

            # Re-evaluate predicates against current final state
            # (VAL-MEM-004).
            av = action_verified(cand, best_evidence_ids)
            ru = reusable(cand)
            nv = (
                cand.source_best_state_id is not None
                and cand.source_best_state_id == final_best_state_id
                and cand.source_best_state_id == best_state_id
            )
            tr = traceable(cand, best_evidence_ids)

            if not (av and ru and nv and tr):
                continue

            # Check idempotency: skip if already has a promoted memory
            # for this source candidate.
            existing = self.repo.list_promoted_memories(task_id)
            if any(p.source_candidate_id == cand.candidate_id for p in existing):
                continue

            # Promote atomically (VAL-MEM-018).
            memory_id = f"prom-{uuid.uuid4().hex[:8]}"
            promoted_memory = PromotedMemory(
                memory_id=memory_id,
                source_candidate_id=cand.candidate_id,
                task_id=task_id,
                content=cand.content,
                memory_type=cand.memory_type,
                layer="task",
                evidence_ids=list(cand.evidence_ids),
                accepted_check_keys=list(cand.accepted_check_keys),
                confidence=cand.confidence,
                created_at=now,
                approved_by="auto",
            )
            updated_candidate = cand.model_copy(
                update={
                    "state": "approved",
                    "status": "approved",
                    "decided_by": "auto",
                    "decision_rationale": "auto_promote",
                    "reviewer": "auto",
                    "reviewed_at": now,
                }
            )
            with self.repo.transaction():
                self.repo.save_memory_candidate(updated_candidate)
                self.repo.save_promoted_memory(promoted_memory)
                self.repo.append_event(
                    EventType.MEMORY_PROMOTED,
                    {
                        "memory_id": memory_id,
                        "source_candidate_id": cand.candidate_id,
                        "approved_by": "auto",
                    },
                    task_id=task_id,
                )
            promoted.append(promoted_memory)

        return promoted
