"""RequirementCompiler for HungerLoop.

The legacy :class:`RuleBasedCompiler` creates a HungerLedger from user goals
and hints, while :class:`RequirementCompiler` extends it with mission-aware
compilation paths. Together they implement invariant I-10 (requirement
compilation).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Literal, cast

from hungerloop.models.enums import (
    AcceptanceCheckType,
    EvidenceType,
    HungerItemStatus,
    HungerItemType,
)
from hungerloop.models.handoff import DiscoveredFact
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.mission import Mission, MissionFeature
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.protocol import RepositoryProtocol


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

    def __init__(self, repo: RepositoryProtocol | None = None) -> None:
        self.repo = repo

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

    def compile_discovered_facts(
        self,
        task_id: str,
        facts: list[DiscoveredFact],
        *,
        budget: BudgetAllocation,
    ) -> list[str]:
        """Compile discovered facts into deterministic ledger items."""
        del budget
        repo = self._require_repo()
        ledger = repo.get_hunger_ledger(task_id)
        items_by_id = {item.id: item for item in ledger.items}
        injected_ids: list[str] = []
        mutated = False

        for fact in facts:
            item_id = self._item_id_for_discovered_fact(fact)
            existing = items_by_id.get(item_id)
            if existing is not None and repo.count_evidence_by_type(
                task_id,
                existing.evidence_ids,
                EvidenceType.DISCOVERED_FACT_COMPILED,
                successful_only=True,
            ) > 0:
                continue

            evidence_payload: dict[str, object] = {
                "success": True,
                "source_handoff_id": fact.source_handoff_id,
                "kind": fact.kind,
                "title": fact.title,
                "description": fact.description,
                "related_feature_ids": list(fact.related_feature_ids),
                "generated_item_id": item_id,
                "ref_handoff_id": fact.source_handoff_id,
            }
            evidence_id = repo.save_evidence(
                task_id=task_id,
                loop_id=None,
                evidence_type=EvidenceType.DISCOVERED_FACT_COMPILED,
                payload=evidence_payload,
            )
            if existing is not None:
                updated_existing = existing.model_copy(
                    update={
                        "evidence_ids": list(
                            dict.fromkeys([*existing.evidence_ids, evidence_id])
                        )
                    }
                )
                items_by_id[item_id] = updated_existing
            else:
                items_by_id[item_id] = HungerItem(
                    id=item_id,
                    title=fact.title,
                    item_type=HungerItemType.GOAL_GAP,
                    priority=0.8,
                    gap_score=1.0,
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.EVIDENCE_COUNT_MIN,
                            params={
                                "evidence_type": "discovered_fact_compiled",
                                "min_count": 1,
                            },
                            description=(
                                f"At least one compiled evidence item for {fact.title}."
                            ),
                        )
                    ],
                    acceptance_mode="all",
                    refinement_tier=0,
                    evidence_ids=[evidence_id],
                    generated_by=fact.source_handoff_id,
                )
                injected_ids.append(item_id)
            mutated = True

        if mutated:
            existing_order = [item.id for item in ledger.items]
            new_ids = [iid for iid in items_by_id if iid not in existing_order]
            repo.save_hunger_ledger(
                task_id,
                HungerLedger(
                    task_id=task_id,
                    items=[
                        *(items_by_id[item_id] for item_id in existing_order),
                        *(items_by_id[item_id] for item_id in new_ids),
                    ],
                ),
            )

        return injected_ids

    def _require_repo(self) -> RepositoryProtocol:
        if self.repo is None:
            raise RuntimeError(
                "RequirementCompiler.compile_discovered_facts requires a repository"
            )
        return self.repo

    @staticmethod
    def _item_id_for_discovered_fact(fact: DiscoveredFact) -> str:
        digest = hashlib.sha1(
            "|".join(
                [
                    fact.kind,
                    fact.title,
                    fact.description,
                    fact.source_handoff_id,
                    ",".join(fact.related_feature_ids),
                ]
            ).encode("utf-8")
        ).hexdigest()[:12].upper()
        return f"H-DISC-{digest}"
