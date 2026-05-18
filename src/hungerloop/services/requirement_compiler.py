"""RequirementCompiler for HungerLoop.

The legacy :class:`RuleBasedCompiler` creates a HungerLedger from user goals
and hints, while :class:`RequirementCompiler` extends it with mission-aware
compilation paths. Together they implement invariant I-10 (requirement
compilation).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Literal, cast

from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    HungerItemType,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.mission import Mission, MissionFeature


class RuleBasedCompiler:
    """Simple rule-based compiler that creates a 2-item or 3-item HungerLedger.

    Creates:
    - H-001: Core deliverable with user-provided acceptance checks
    - H-002: Evidence requirement (EVIDENCE_COUNT_MIN >= 1)
    - H-003: Memory consolidation (HUMAN_APPROVAL) if enabled
    """

    def compile(
        self,
        task_id: str,
        raw_goal: str,
        hints: dict[str, Any] | None = None,
    ) -> tuple[str, HungerLedger]:
        """Compile a raw goal into a HungerLedger.

        Args:
            task_id: Unique task identifier
            raw_goal: User's goal description
            hints: Optional hints dictionary containing:
                - core_acceptance_checks: List of acceptance check dicts (required)
                - core_acceptance_mode: "all" or "any" (default: "all")
                - enable_memory_consolidation: bool (default: False)

        Returns:
            Tuple of (raw_goal, HungerLedger)

        Raises:
            ValueError: If core_acceptance_checks is missing
        """
        hints = hints or {}
        items: list[HungerItem] = []

        core_checks = hints.get("core_acceptance_checks")
        if not core_checks or not isinstance(core_checks, list):
            raise ValueError("MVP requires core_acceptance_checks.")

        mode_str = str(hints.get("core_acceptance_mode", "all"))
        mode: Literal["all", "any"] = cast(
            Literal["all", "any"], mode_str if mode_str in ("all", "any") else "all"
        )

        items.append(
            HungerItem(
                id="H-001",
                title="Core deliverable",
                item_type=HungerItemType.GOAL_GAP,
                priority=1.0,
                gap_score=1.0,
                acceptance_checks=[
                    AcceptanceCheck(**c) for c in core_checks
                ],
                acceptance_mode=mode,
            )
        )

        items.append(
            HungerItem(
                id="H-002",
                title="Sufficient evidence",
                item_type=HungerItemType.GOAL_GAP,
                priority=0.7,
                gap_score=1.0,
                acceptance_checks=[
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.EVIDENCE_COUNT_MIN,
                        params={"evidence_type": "any", "min_count": 1},
                        description="At least one evidence item.",
                    )
                ],
                acceptance_mode="all",
            )
        )

        if hints.get("enable_memory_consolidation", False):
            items.append(
                HungerItem(
                    id="H-003",
                    title="Memory consolidation",
                    item_type=HungerItemType.MEMORY_CONSOLIDATION,
                    priority=0.4,
                    gap_score=1.0,
                    status=HungerItemStatus.OPEN,
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.HUMAN_APPROVAL,
                            params={"approval_id": f"{task_id}-memory"},
                            description="Human approves promoted memory.",
                        )
                    ],
                    acceptance_mode="all",
                )
            )

        return raw_goal, HungerLedger(task_id=task_id, items=items)


class RequirementCompiler(RuleBasedCompiler):
    """Mission-aware requirement compiler for v0.6 features."""

    def compile_mission_features(self, task_id: str, mission: Mission) -> HungerLedger:
        """Project mission features into a deterministic hunger ledger."""
        features_per_phase = Counter(
            feature.phase_id for feature in mission.features
        )
        items = [
            HungerItem(
                id=feature.hunger_item_id,
                title=feature.title,
                item_type=HungerItemType.GOAL_GAP,
                priority=1.0 / max(1, features_per_phase.get(feature.phase_id, 0)),
                gap_score=1.0,
                acceptance_checks=self.compile_checks_for_feature(feature),
                acceptance_mode="all",
                refinement_tier=0,
            )
            for feature in mission.features
        ]
        return HungerLedger(task_id=task_id, items=items)

    def compile_checks_for_feature(
        self,
        feature: MissionFeature,
    ) -> list[AcceptanceCheck]:
        """Compile default acceptance checks for a mission feature."""
        return [
            AcceptanceCheck(
                check_type=AcceptanceCheckType.EVIDENCE_COUNT_MIN,
                params={"evidence_type": "any", "min_count": 1},
                description=f"At least one evidence item for {feature.feature_id}.",
            )
        ]
