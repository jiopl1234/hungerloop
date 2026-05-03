"""SkillManager — generate :class:`SkillCard` from a finished task (PRD §20).

PRD §20.2 fixes a strict trigger rule so we never produce vague skill cards:

    Generate SkillCard only when:
      stop_reason == DONE
      and best is not None
      and len(best.accepted_check_keys) >= 2

Skill generation is intentionally end-of-task only (the orchestrator does
not call this per loop). The CLI ``run`` command invokes
:meth:`SkillManager.maybe_create_skill_card` after :meth:`LoopOrchestrator.run`
returns and before the StopReport is persisted.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hungerloop.models.enums import StopReason
from hungerloop.models.skill import SkillCard
from hungerloop.models.tracing import StopReport
from hungerloop.repository.protocol import RepositoryProtocol


class SkillManager:
    """Create skill cards on successful (DONE + ≥2 accepted checks) tasks."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def maybe_create_skill_card(
        self, task_id: str, stop_report: StopReport
    ) -> SkillCard | None:
        """Return a saved :class:`SkillCard` or ``None`` if the trigger fails."""
        if stop_report.stop_reason != StopReason.DONE:
            return None

        best = self.repo.get_best_state(task_id)
        if best is None or len(best.accepted_check_keys) < 2:
            return None

        skill_id = f"skill-{uuid.uuid4().hex[:8]}"
        card = SkillCard(
            skill_id=skill_id,
            task_id=task_id,
            name=f"task-{task_id}",
            task_description=stop_report.summary or stop_report.recommendation,
            accepted_check_keys=list(best.accepted_check_keys),
            evidence_ids=list(best.evidence_ids),
            artifacts=list(best.artifact_ids),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.repo.save_skill_card(card)
        return card
