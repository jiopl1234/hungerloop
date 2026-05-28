"""RequirementCompiler for HungerLoop.

The legacy :class:`RuleBasedCompiler` creates a HungerLedger from user goals
and hints, while :class:`RequirementCompiler` extends it with mission-aware
compilation paths. Together they implement invariant I-10 (requirement
compilation).
"""
from __future__ import annotations

import hashlib
import shlex
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from hungerloop.models.enums import (
    AcceptanceCheckType,
    EvidenceType,
    HungerItemStatus,
    HungerItemType,
)
from hungerloop.models.handoff import DiscoveredFact
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationContract,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.mission_loader import ParsedMissionSpec

MISSION_IMPORT_EVIDENCE_TYPE = "mission_import"
_MISSION_COMPARISON_FIELDS = (
    "title",
    "description",
    "max_parallel_features",
    "services_manifest",
)
_PHASE_COMPARISON_FIELDS = (
    "title",
    "description",
    "feature_ids",
    "validation_contract_ids",
)
_FEATURE_COMPARISON_FIELDS = (
    "hunger_item_id",
    "phase_id",
    "title",
    "description",
    "preconditions",
    "expected_behavior",
    "verification_steps",
    "fulfills",
)
_ASSERTION_COMPARISON_FIELDS = (
    "phase_id",
    "title",
    "description",
    "check_type",
    "params",
    "evidence_requirements",
)
_OP_PAST_TENSE = {"add": "added", "update": "updated", "remove": "removed"}
_FILE_VERIFICATION_PREFIXES = (
    "file exists:",
    "file_exists:",
    "exists:",
)
_COMMAND_VERIFICATION_PREFIXES = (
    "command:",
    "run:",
    "shell:",
)
_COMMAND_TIMEOUT_PREFIX = "timeout="


class MissionChangeOperation(BaseModel):
    """Deterministic add/update/remove operation emitted by mission import."""

    op: Literal["add", "update", "remove"]
    entity_type: Literal["mission", "phase", "feature", "assertion"]
    entity_id: str
    fields: list[str] = Field(default_factory=list)


class MissionChangeResult(BaseModel):
    """Result returned by ``compile_mission_changes`` for CLI summaries."""

    mission: Mission
    validation_contract: ValidationContract | None = None
    hunger_ledger: HungerLedger
    operations: list[MissionChangeOperation]
    evidence_id: str
    summary: dict[str, int]


class MissionCreationResult(BaseModel):
    """Result returned by ``compile_new_mission`` for CLI creation."""

    mission: Mission
    validation_contract: ValidationContract | None = None
    hunger_ledger: HungerLedger


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

    def compile_new_mission(
        self,
        task_id: str,
        parsed_spec: ParsedMissionSpec,
    ) -> MissionCreationResult:
        """Persist a newly-created mission through the compiler write path."""
        repo = self._require_repo("compile_new_mission")
        existing_mission = repo.get_mission(task_id)
        if existing_mission is not None:
            raise ValueError(
                "mission already exists for task; use 'mission import' to update"
            )
        mission = self._build_imported_mission(
            task_id=task_id,
            mission_id=f"mission-{task_id}",
            parsed_spec=parsed_spec,
            existing_mission=None,
        )
        contract = self._build_imported_contract(
            mission_id=mission.mission_id,
            parsed_spec=parsed_spec,
            existing_contract=None,
        )
        self._validate_assertion_phase_ids(mission, contract)
        ledger = self.compile_mission_features(task_id, mission)

        with repo.transaction():
            repo.save_mission(mission)
            if contract is not None:
                repo.save_validation_contract(contract)
            repo.save_hunger_ledger(task_id, ledger)

        return MissionCreationResult(
            mission=mission,
            validation_contract=contract,
            hunger_ledger=ledger,
        )

    def compile_mission_changes(
        self,
        task_id: str,
        parsed_spec: ParsedMissionSpec,
    ) -> MissionChangeResult:
        """Apply a parsed mission import through the compiler write path.

        REQ-M6-054 makes this method the single route from imported operator
        specs to mission SQLite state. It diffs by stable ids, preserves runtime
        status/evidence for unchanged entities, updates the hunger ledger, and
        emits one successful ``mission_import`` evidence row listing changes.
        """
        repo = self._require_repo("compile_mission_changes")
        existing_mission = repo.get_mission(task_id)
        mission_id = (
            existing_mission.mission_id
            if existing_mission is not None
            else f"mission-{task_id}"
        )
        existing_contract = (
            repo.get_validation_contract(mission_id)
            if existing_mission is not None
            else None
        )

        mission = self._build_imported_mission(
            task_id=task_id,
            mission_id=mission_id,
            parsed_spec=parsed_spec,
            existing_mission=existing_mission,
        )
        contract = self._build_imported_contract(
            mission_id=mission_id,
            parsed_spec=parsed_spec,
            existing_contract=existing_contract,
        )
        self._validate_assertion_phase_ids(mission, contract)
        operations = [
            *self._diff_mission(existing_mission, mission),
            *self._diff_contract(existing_contract, contract),
        ]
        ledger = self._merge_imported_ledger(task_id, mission)
        summary = self._summarize_operations(operations)
        evidence_payload: dict[str, object] = {
            "success": True,
            "kind": MISSION_IMPORT_EVIDENCE_TYPE,
            "mission_id": mission.mission_id,
            "source_files": list(parsed_spec.source_files),
            "changes": [
                operation.model_dump(mode="json") for operation in operations
            ],
            "summary": summary,
        }

        with repo.transaction():
            if existing_contract is not None:
                repo.save_validation_contract(
                    ValidationContract(mission_id=mission.mission_id)
                )
            repo.save_mission(mission)
            if contract is not None:
                repo.save_validation_contract(contract)
            repo.save_hunger_ledger(task_id, ledger)
            evidence_id = repo.save_evidence(
                task_id=task_id,
                loop_id=None,
                evidence_type=EvidenceType.HUMAN_INPUT,
                payload=evidence_payload,
            )

        return MissionChangeResult(
            mission=mission,
            validation_contract=contract,
            hunger_ledger=ledger,
            operations=operations,
            evidence_id=evidence_id,
            summary=summary,
        )

    def compile_checks_for_feature(
        self,
        feature: MissionFeature,
    ) -> list[AcceptanceCheck]:
        """Compile feature verification steps into deterministic checks.

        Recognised prefixes (case-insensitive):
        - ``file exists:`` / ``file_exists:`` / ``exists:`` -> FILE_EXISTS
        - ``command:`` / ``run:`` / ``shell:`` -> SHELL_EXIT_ZERO

        Steps without a recognised prefix are skipped to avoid false
        positives from natural-language descriptions that happen to
        begin with a tool name (e.g. ``"Python 3.11+ syntax"``).
        """
        checks: list[AcceptanceCheck] = []
        for step in feature.verification_steps:
            normalized = step.strip()
            lowered = normalized.lower()
            file_path = _prefixed_value(normalized, lowered, _FILE_VERIFICATION_PREFIXES)
            if file_path is not None:
                checks.append(
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.FILE_EXISTS,
                        params={"path": file_path},
                        description=normalized,
                    )
                )
                continue

            argv_text = _prefixed_value(
                normalized,
                lowered,
                _COMMAND_VERIFICATION_PREFIXES,
            )
            if argv_text is not None:
                argv, timeout = _command_argv_and_timeout(argv_text)
                if argv:
                    params: dict[str, Any] = {"argv": argv}
                    if timeout is not None:
                        params["timeout"] = timeout
                    checks.append(
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                            params=params,
                            description=normalized,
                        )
                    )
                continue

        if checks:
            return checks

        return [
            AcceptanceCheck(
                check_type=AcceptanceCheckType.HUMAN_APPROVAL,
                params={"approval_id": f"{feature.feature_id}-verification"},
                description=(
                    "Manual verification required for "
                    f"{feature.feature_id}; add verification_steps with "
                    "'file exists:' or 'command:' for automated checks."
                ),
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

    def _build_imported_mission(
        self,
        *,
        task_id: str,
        mission_id: str,
        parsed_spec: ParsedMissionSpec,
        existing_mission: Mission | None,
    ) -> Mission:
        existing_phases = (
            {phase.phase_id: phase for phase in existing_mission.phases}
            if existing_mission is not None
            else {}
        )
        existing_features = (
            {feature.feature_id: feature for feature in existing_mission.features}
            if existing_mission is not None
            else {}
        )
        imported_features = (
            [
                self._merge_feature_runtime(
                    imported=feature,
                    existing=existing_features.get(feature.feature_id),
                )
                for feature in parsed_spec.features
            ]
            if parsed_spec.features is not None
            else list(existing_features.values())
        )

        if parsed_spec.phases is not None:
            imported_phases = [
                self._merge_phase_runtime(
                    imported=phase,
                    existing=existing_phases.get(phase.phase_id),
                    features=imported_features,
                    assertions=(
                        parsed_spec.validation_contract.assertions
                        if parsed_spec.validation_contract is not None
                        else None
                    ),
                )
                for phase in parsed_spec.phases
            ]
        elif existing_mission is not None:
            imported_phases = list(existing_mission.phases)
        else:
            imported_phases = self._phases_from_imported_children(
                imported_features,
                (
                    parsed_spec.validation_contract.assertions
                    if parsed_spec.validation_contract is not None
                    else []
                ),
            )

        created_at = (
            existing_mission.created_at
            if existing_mission is not None
            else datetime.now(timezone.utc)
        )
        return Mission(
            mission_id=mission_id,
            task_id=task_id,
            title=(
                parsed_spec.title
                if _has_mission_markdown(parsed_spec) or existing_mission is None
                else existing_mission.title
            ),
            description=(
                parsed_spec.description
                if _has_mission_markdown(parsed_spec) or existing_mission is None
                else existing_mission.description
            ),
            phases=imported_phases,
            features=imported_features,
            created_at=created_at,
            started_at=existing_mission.started_at if existing_mission else None,
            completed_at=existing_mission.completed_at if existing_mission else None,
            max_parallel_features=(
                existing_mission.max_parallel_features if existing_mission else None
            ),
            services_manifest=(
                parsed_spec.services_manifest
                if parsed_spec.services_manifest is not None
                else (
                    existing_mission.services_manifest
                    if existing_mission is not None
                    else None
                )
            ),
        )

    @staticmethod
    def _merge_feature_runtime(
        *,
        imported: MissionFeature,
        existing: MissionFeature | None,
    ) -> MissionFeature:
        if existing is None:
            return imported
        return imported.model_copy(
            update={
                "status": existing.status,
                "assigned_worker_ids": list(existing.assigned_worker_ids),
            }
        )

    @staticmethod
    def _merge_phase_runtime(
        *,
        imported: MissionPhase,
        existing: MissionPhase | None,
        features: list[MissionFeature],
        assertions: list[ValidationAssertion] | None,
    ) -> MissionPhase:
        feature_ids = _ordered_child_ids(
            imported.feature_ids,
            [
                feature.feature_id
                for feature in features
                if feature.phase_id == imported.phase_id
            ],
        )
        assertion_ids = _ordered_child_ids(
            imported.validation_contract_ids,
            [
                assertion.assertion_id
                for assertion in assertions or []
                if assertion.phase_id == imported.phase_id
            ],
        )
        if existing is None:
            return imported.model_copy(
                update={
                    "feature_ids": feature_ids,
                    "validation_contract_ids": assertion_ids,
                }
            )
        return imported.model_copy(
            update={
                "feature_ids": feature_ids,
                "validation_contract_ids": assertion_ids,
                "status": existing.status,
                "completed_at": existing.completed_at,
            }
        )

    @staticmethod
    def _phases_from_imported_children(
        features: list[MissionFeature],
        assertions: list[ValidationAssertion],
    ) -> list[MissionPhase]:
        phase_ids = list(
            dict.fromkeys(
                [
                    *(feature.phase_id for feature in features),
                    *(assertion.phase_id for assertion in assertions),
                ]
            )
        )
        return [
            MissionPhase(
                phase_id=phase_id,
                title=phase_id,
                description="",
                feature_ids=[
                    feature.feature_id
                    for feature in features
                    if feature.phase_id == phase_id
                ],
                validation_contract_ids=[
                    assertion.assertion_id
                    for assertion in assertions
                    if assertion.phase_id == phase_id
                ],
            )
            for phase_id in phase_ids
        ]

    @staticmethod
    def _build_imported_contract(
        *,
        mission_id: str,
        parsed_spec: ParsedMissionSpec,
        existing_contract: ValidationContract | None,
    ) -> ValidationContract | None:
        if parsed_spec.validation_contract is None:
            return existing_contract
        existing_assertions = (
            {
                assertion.assertion_id: assertion
                for assertion in existing_contract.assertions
            }
            if existing_contract is not None
            else {}
        )
        imported_assertions = [
            _merge_assertion_runtime(
                imported=assertion,
                existing=existing_assertions.get(assertion.assertion_id),
            )
            for assertion in parsed_spec.validation_contract.assertions
        ]
        return ValidationContract(
            mission_id=mission_id,
            assertions=imported_assertions,
        )

    @staticmethod
    def _validate_assertion_phase_ids(
        mission: Mission,
        contract: ValidationContract | None,
    ) -> None:
        if contract is None:
            return
        phase_ids = {phase.phase_id for phase in mission.phases}
        for assertion in contract.assertions:
            if assertion.phase_id not in phase_ids:
                available = ", ".join(sorted(phase_ids)) or "<none>"
                raise ValueError(
                    "validation assertion "
                    f"{assertion.assertion_id!r} references unknown "
                    f"phase_id {assertion.phase_id!r}; "
                    f"available phase_ids: {available}"
                )

    def _merge_imported_ledger(
        self,
        task_id: str,
        mission: Mission,
    ) -> HungerLedger:
        repo = self._require_repo("compile_mission_changes")
        existing_ledger = repo.get_hunger_ledger(task_id)
        compiled = self.compile_mission_features(task_id, mission)
        existing_by_id = {item.id: item for item in existing_ledger.items}
        compiled_by_id = {item.id: item for item in compiled.items}
        imported_hunger_ids = {feature.hunger_item_id for feature in mission.features}

        merged_by_id: dict[str, HungerItem] = {}
        for compiled_item in compiled.items:
            existing_item = existing_by_id.get(compiled_item.id)
            if existing_item is None:
                merged_by_id[compiled_item.id] = compiled_item
            else:
                merged_by_id[compiled_item.id] = compiled_item.model_copy(
                    update={
                        "status": existing_item.status,
                        "consecutive_failure_count": (
                            existing_item.consecutive_failure_count
                        ),
                        "last_progress_loop_id": existing_item.last_progress_loop_id,
                        "evidence_ids": list(existing_item.evidence_ids),
                        "updated_at_loop": existing_item.updated_at_loop,
                        "refinement_tier": existing_item.refinement_tier,
                        "refinement_kind": existing_item.refinement_kind,
                        "generated_by": existing_item.generated_by,
                        "source_check_keys": list(existing_item.source_check_keys),
                    }
                )

        ordered_items: list[HungerItem] = []
        for item in existing_ledger.items:
            if item.id in compiled_by_id:
                ordered_items.append(merged_by_id[item.id])
            elif item.id not in imported_hunger_ids and _should_preserve_item(item):
                ordered_items.append(item)
        ordered_items.extend(
            merged_by_id[item.id]
            for item in compiled.items
            if item.id not in {existing_item.id for existing_item in ordered_items}
        )
        return HungerLedger(task_id=task_id, items=ordered_items)

    @staticmethod
    def _diff_mission(
        before: Mission | None,
        after: Mission,
    ) -> list[MissionChangeOperation]:
        operations: list[MissionChangeOperation] = []
        if before is None:
            operations.append(
                MissionChangeOperation(
                    op="add",
                    entity_type="mission",
                    entity_id=after.mission_id,
                    fields=list(_MISSION_COMPARISON_FIELDS),
                )
            )
        else:
            changed_fields = _changed_fields(
                before,
                after,
                _MISSION_COMPARISON_FIELDS,
            )
            if changed_fields:
                operations.append(
                    MissionChangeOperation(
                        op="update",
                        entity_type="mission",
                        entity_id=after.mission_id,
                        fields=changed_fields,
                    )
                )
        operations.extend(
            _diff_entities(
                entity_type="phase",
                before={
                    phase.phase_id: phase
                    for phase in before.phases
                }
                if before is not None
                else {},
                after={phase.phase_id: phase for phase in after.phases},
                fields=_PHASE_COMPARISON_FIELDS,
            )
        )
        operations.extend(
            _diff_entities(
                entity_type="feature",
                before={
                    feature.feature_id: feature
                    for feature in before.features
                }
                if before is not None
                else {},
                after={feature.feature_id: feature for feature in after.features},
                fields=_FEATURE_COMPARISON_FIELDS,
            )
        )
        return operations

    @staticmethod
    def _diff_contract(
        before: ValidationContract | None,
        after: ValidationContract | None,
    ) -> list[MissionChangeOperation]:
        return _diff_entities(
            entity_type="assertion",
            before={
                assertion.assertion_id: assertion
                for assertion in before.assertions
            }
            if before is not None
            else {},
            after={
                assertion.assertion_id: assertion
                for assertion in after.assertions
            }
            if after is not None
            else {},
            fields=_ASSERTION_COMPARISON_FIELDS,
        )

    @staticmethod
    def _summarize_operations(
        operations: list[MissionChangeOperation],
    ) -> dict[str, int]:
        entity_names = {
            "mission": "missions",
            "phase": "phases",
            "feature": "features",
            "assertion": "assertions",
        }
        summary: dict[str, int] = {}
        for entity_type, plural in entity_names.items():
            for op in ("add", "update", "remove"):
                key = (
                    f"{plural}_{_OP_PAST_TENSE[op]}"
                    if op != "remove"
                    else f"{plural}_removed"
                )
                summary[key] = sum(
                    1
                    for operation in operations
                    if operation.entity_type == entity_type and operation.op == op
                )
        return summary

    def _require_repo(self, method_name: str = "compile_discovered_facts") -> RepositoryProtocol:
        if self.repo is None:
            raise RuntimeError(
                f"RequirementCompiler.{method_name} requires a repository"
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


def _merge_assertion_runtime(
    *,
    imported: ValidationAssertion,
    existing: ValidationAssertion | None,
) -> ValidationAssertion:
    if existing is None:
        return imported
    return imported.model_copy(
        update={
            "status": existing.status,
            "validated_at_loop": existing.validated_at_loop,
            "evidence_ids": list(existing.evidence_ids),
        }
    )


def _has_mission_markdown(parsed_spec: ParsedMissionSpec) -> bool:
    return parsed_spec.mission_markdown_loaded or "mission.md" in parsed_spec.source_files


def _ordered_child_ids(existing: list[str], desired: list[str]) -> list[str]:
    desired_set = set(desired)
    ordered = [child_id for child_id in existing if child_id in desired_set]
    ordered.extend(child_id for child_id in desired if child_id not in ordered)
    return ordered


def _should_preserve_item(item: HungerItem) -> bool:
    return item.generated_by is not None or item.id.startswith("H-DISC-")


def _prefixed_value(
    original: str,
    lowered: str,
    prefixes: tuple[str, ...],
) -> str | None:
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return original[len(prefix) :].strip()
    return None


def _command_argv_and_timeout(argv_text: str) -> tuple[list[str], int | None]:
    parts = shlex.split(argv_text)
    if not parts:
        return [], None
    last = parts[-1]
    if not last.startswith(_COMMAND_TIMEOUT_PREFIX):
        return parts, None
    timeout_text = last[len(_COMMAND_TIMEOUT_PREFIX) :]
    try:
        timeout = int(timeout_text)
    except ValueError:
        return parts, None
    if timeout <= 0:
        return parts, None
    del parts[-1]
    return parts, timeout


def _changed_fields(
    before: BaseModel,
    after: BaseModel,
    fields: tuple[str, ...],
) -> list[str]:
    return [
        field
        for field in fields
        if getattr(before, field) != getattr(after, field)
    ]


def _diff_entities(
    *,
    entity_type: Literal["phase", "feature", "assertion"],
    before: dict[str, BaseModel],
    after: dict[str, BaseModel],
    fields: tuple[str, ...],
) -> list[MissionChangeOperation]:
    operations: list[MissionChangeOperation] = []
    for entity_id in sorted(set(after) - set(before)):
        operations.append(
            MissionChangeOperation(
                op="add",
                entity_type=entity_type,
                entity_id=entity_id,
                fields=list(fields),
            )
        )
    for entity_id in sorted(set(before) & set(after)):
        changed = _changed_fields(before[entity_id], after[entity_id], fields)
        if changed:
            operations.append(
                MissionChangeOperation(
                    op="update",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    fields=changed,
                )
            )
    for entity_id in sorted(set(before) - set(after)):
        operations.append(
            MissionChangeOperation(
                op="remove",
                entity_type=entity_type,
                entity_id=entity_id,
                fields=[],
            )
        )
    return operations
