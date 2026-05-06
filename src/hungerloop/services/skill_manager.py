"""SkillManager — generate :class:`SkillCardCandidate` from a finished task.

PRD §15.2 / §18 trigger rule (v0.5e.1):

    Generate a SkillCardCandidate (NOT an active SkillCard) only when
    ALL of the following hold:

      1. stop_report.stop_reason == StopReason.DONE
      2. final BestState exists for the task
      3. len(best.accepted_check_keys) >= 2
      4. at least one tool_call_succeeded evidence row exists
      5. the final committed LoopTrace's regressed_check_keys is empty

If any condition fails, we return ``None`` and emit no event. Operators
review the candidate via ``hungerloop skill approve`` to promote it
into an :class:`ActiveSkillCard`.

The shipped legacy ``save_skill_card`` path remains functional for any
test that constructs ``SkillCard`` directly, but ``SkillManager``
itself no longer uses it (PRD §18 / FR-8).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hungerloop.models.enums import EvidenceType, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.skill import SkillCardCandidate
from hungerloop.models.tracing import StopReport
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.skill_derivation import (
    derive_failures,
    derive_name,
    derive_preconditions,
    derive_steps,
    derive_tools,
    derive_trigger_signals,
)


class SkillManager:
    """Auto-generate :class:`SkillCardCandidate` rows on DONE tasks."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def maybe_create_skill_card(
        self, task_id: str, stop_report: StopReport
    ) -> SkillCardCandidate | None:
        """Return a saved candidate, or ``None`` when the trigger fails.

        v0.5e.1 keeps the historical ``maybe_create_skill_card`` name
        and signature so the orchestrator + run CLI don't need to
        learn a new entry point — but the return type is now
        :class:`SkillCardCandidate`.
        """
        if not self._trigger_fires(task_id, stop_report):
            return None

        best = self.repo.get_best_state(task_id)
        # _trigger_fires already proved best is non-None and has the
        # accepted_check_keys count we need; this re-fetch is just so
        # mypy sees the narrowed type below.
        assert best is not None

        traces = self.repo.list_loop_traces(task_id)

        candidate_id = f"skill-cand-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).replace(microsecond=0)

        candidate = SkillCardCandidate(
            skill_candidate_id=candidate_id,
            task_id=task_id,
            source_best_state_id=best.state_id,
            source_stop_report_id=None,
            name=derive_name(best),
            description=stop_report.summary or stop_report.recommendation,
            trigger_signals=derive_trigger_signals(best, traces),
            preconditions=derive_preconditions(best, traces),
            steps=derive_steps(traces),
            tools_used=derive_tools(traces, self.repo),
            accepted_check_keys=list(best.accepted_check_keys),
            artifact_ids=list(best.artifact_ids),
            evidence_ids=list(best.evidence_ids),
            known_failures=derive_failures(task_id, self.repo),
            reuse_notes=[],
            state="candidate",
            created_at=now,
        )
        self.repo.save_skill_card_candidate(candidate)
        self.repo.append_event(
            EventType.SKILL_CARD_CANDIDATE_CREATED,
            {
                "skill_candidate_id": candidate_id,
                "task_id": task_id,
                "source_best_state_id": best.state_id,
                "name": candidate.name,
            },
            task_id=task_id,
        )
        return candidate

    def _trigger_fires(
        self, task_id: str, stop_report: StopReport
    ) -> bool:
        """All five PRD §15.2 conditions; short-circuits on the first
        failure for performance + log clarity."""
        if stop_report.stop_reason is not StopReason.DONE:
            return False
        best = self.repo.get_best_state(task_id)
        if best is None or len(best.accepted_check_keys) < 2:
            return False
        # Condition 4 — any successful tool_call evidence row.
        if not self.repo.list_successful_tool_call_evidence(task_id):
            # Cheap fallback: count_evidence_by_type with successful_only.
            # Some test fixtures bypass list_successful_tool_call_evidence
            # by writing evidence rows that don't carry the 'success'
            # marker; the count_evidence_by_type path knows the same
            # shape.
            count = self.repo.count_evidence_by_type(
                task_id,
                evidence_ids=[],
                evidence_type=EvidenceType.TOOL_CALL,
                successful_only=True,
            )
            if count == 0:
                return False
        # Condition 5 — final committed trace has no regressions.
        traces = self.repo.list_loop_traces(task_id)
        committed = [t for t in traces if t.committed]
        if not committed:
            return False
        final = committed[-1]
        regressed = getattr(final, "regressed_check_keys", []) or []
        if regressed:
            return False
        return True
