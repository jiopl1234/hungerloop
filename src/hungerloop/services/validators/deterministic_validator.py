"""Zero-behavior deterministic validation wrapper for HungerLoop v0.6.

``DeterministicValidator`` implements REQ-M4-020 and REQ-M4-021 by delegating
directly to :class:`hungerloop.services.validation_gate.ValidationGate` without
adding fields, side effects, or rejection rules.
"""
from __future__ import annotations

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.validation import ValidationReport
from hungerloop.services.validation_gate import ValidationGate


class DeterministicValidator:
    """Thin adapter around the existing v0.5f validation gate."""

    def __init__(self, validation_gate: ValidationGate) -> None:
        self.validation_gate = validation_gate

    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
        target_hunger_item_ids: list[str],
    ) -> ValidationReport:
        """Delegate to ``ValidationGate.validate`` with byte-equivalent output."""
        return await self.validation_gate.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            target_hunger_item_ids=target_hunger_item_ids,
        )
