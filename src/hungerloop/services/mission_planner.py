"""Mission-aware planner for HungerLoop v0.6 M3 (REQ-M3-010..020)."""
from __future__ import annotations

from dataclasses import dataclass

from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.hunger import HungerItem, HungerSnapshot
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import (
    Assignment,
    BudgetAllocation,
    LoopPlan,
    WorkerRole,
)
from hungerloop.models.worker import WorkerHandoff
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.agent_registry import EXECUTION_WORKER_V1
from hungerloop.services.rule_based_planner import RuleBasedPlanner

__all__ = ["MissionPlanner", "PlannerCycleError", "WorkerRole"]

_ACTIVE_FEATURE_STATUSES = frozenset({"pending", "in_progress"})
_ACTIVE_PHASE_STATUSES = frozenset({"pending", "in_progress"})
_ACTIVE_HUNGER_STATUSES = frozenset(
    {HungerItemStatus.OPEN, HungerItemStatus.WORKING}
)
_BLOCKER_ITEM_TYPE = "blocker"
_NO_CANDIDATES_RATIONALE = "no candidate features"


class PlannerCycleError(ValueError):
    """Raised when mission assignment dependencies contain a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(
            "Planner dependency cycle detected: " + " -> ".join(cycle)
        )


@dataclass(frozen=True)
class _CandidateFeature:
    feature: MissionFeature
    item: HungerItem
    phase: MissionPhase
    base_index: int

    @property
    def score(self) -> float:
        return self.item.priority * self.item.gap_score


class MissionPlanner:
    """Build deterministic loop plans for legacy and mission-runtime tasks."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        legacy_planner: RuleBasedPlanner | None = None,
    ) -> None:
        self.repo = repo
        self._legacy_planner = legacy_planner or RuleBasedPlanner(repo)

    def plan(
        self,
        task_id: str,
        loop_id: int,
        snapshot: HungerSnapshot,
        budget: BudgetAllocation,
        mission: Mission | None = None,
        prior_handoffs: list[WorkerHandoff] | None = None,
    ) -> LoopPlan:
        """Build a loop plan, delegating unchanged to v0.5f when missionless."""
        if mission is None:
            return self._legacy_planner.plan(task_id, loop_id, snapshot, budget)

        ledger = self.repo.get_hunger_ledger(task_id)
        item_by_id = {item.id: item for item in ledger.items}
        phase_by_id = {phase.phase_id: phase for phase in mission.phases}
        blocked_feature_ids, blocked_hunger_item_ids = self._blocked_targets(
            prior_handoffs or []
        )

        candidates: list[_CandidateFeature] = []
        skip_reasons: dict[str, str] = {}
        for index, feature in enumerate(mission.features):
            candidate = self._candidate_for_feature(
                feature=feature,
                base_index=index,
                item_by_id=item_by_id,
                phase_by_id=phase_by_id,
                blocked_feature_ids=blocked_feature_ids,
                blocked_hunger_item_ids=blocked_hunger_item_ids,
            )
            if isinstance(candidate, str):
                skip_reasons[feature.feature_id] = candidate
            else:
                candidates.append(candidate)

        sorted_candidates = sorted(candidates, key=self._candidate_sort_key)
        candidate_by_feature_id = {
            candidate.feature.feature_id: candidate for candidate in sorted_candidates
        }
        dependencies_by_feature = self._dependencies_by_feature(
            [candidate.feature for candidate in sorted_candidates]
        )
        self._raise_for_cycles(
            dependencies_by_feature,
            [candidate.feature.feature_id for candidate in sorted_candidates],
        )

        max_parallel_features = mission.max_parallel_features or 1
        selected_count = min(
            budget.max_workers_per_loop,
            len(sorted_candidates),
            max_parallel_features,
        )
        selected_feature_ids = self._select_feature_ids(
            sorted_candidates,
            dependencies_by_feature,
            selected_count,
        )
        selected_feature_id_set = set(selected_feature_ids)
        for candidate in sorted_candidates:
            feature_id = candidate.feature.feature_id
            if feature_id not in selected_feature_id_set:
                skip_reasons[feature_id] = "not_selected_budget_cap"

        ordered_feature_ids = self._topological_order(
            selected_feature_ids,
            dependencies_by_feature,
        )
        ordered_candidates = [
            candidate_by_feature_id[feature_id] for feature_id in ordered_feature_ids
        ]
        assignment_id_by_feature_id = {
            candidate.feature.feature_id: f"ASGN-{task_id}-{loop_id}-{index}"
            for index, candidate in enumerate(ordered_candidates)
        }

        assignments = [
            self._assignment_for_candidate(
                task_id=task_id,
                loop_id=loop_id,
                index=index,
                mission=mission,
                candidate=candidate,
                dependencies_by_feature=dependencies_by_feature,
                assignment_id_by_feature_id=assignment_id_by_feature_id,
                selected_feature_ids=selected_feature_id_set,
                budget=budget,
            )
            for index, candidate in enumerate(ordered_candidates)
        ]
        rationale = self._rationale(
            mission=mission,
            selected_feature_ids=selected_feature_id_set,
            skip_reasons=skip_reasons,
            item_by_id=item_by_id,
        )

        return LoopPlan(
            task_id=task_id,
            loop_id=loop_id,
            selected_hunger_item_ids=[
                candidate.feature.hunger_item_id for candidate in ordered_candidates
            ],
            assignments=assignments,
            phase=budget.phase,
            rationale=rationale,
        )

    @staticmethod
    def _candidate_sort_key(candidate: _CandidateFeature) -> tuple[int, float, str]:
        return (
            candidate.item.refinement_tier,
            -candidate.score,
            candidate.feature.feature_id,
        )

    @staticmethod
    def _blocked_targets(
        prior_handoffs: list[WorkerHandoff],
    ) -> tuple[frozenset[str], frozenset[str]]:
        feature_ids: set[str] = set()
        hunger_item_ids: set[str] = set()
        for handoff in prior_handoffs:
            for item in handoff.handoff_items:
                if item.item_type != _BLOCKER_ITEM_TYPE:
                    continue
                feature_ids.update(item.related_feature_ids)
                hunger_item_ids.update(item.related_item_ids)
        return frozenset(feature_ids), frozenset(hunger_item_ids)

    @staticmethod
    def _candidate_for_feature(
        *,
        feature: MissionFeature,
        base_index: int,
        item_by_id: dict[str, HungerItem],
        phase_by_id: dict[str, MissionPhase],
        blocked_feature_ids: frozenset[str],
        blocked_hunger_item_ids: frozenset[str],
    ) -> _CandidateFeature | str:
        if feature.status not in _ACTIVE_FEATURE_STATUSES:
            return f"feature_status_{feature.status}"
        if (
            feature.feature_id in blocked_feature_ids
            or feature.hunger_item_id in blocked_hunger_item_ids
        ):
            return "blocked_by_prior_handoff"

        phase = phase_by_id.get(feature.phase_id)
        if phase is None:
            return "phase_missing"
        if phase.status not in _ACTIVE_PHASE_STATUSES:
            return f"phase_status_{phase.status}"

        item = item_by_id.get(feature.hunger_item_id)
        if item is None:
            return "hunger_item_missing"
        if item.status not in _ACTIVE_HUNGER_STATUSES:
            return f"hunger_status_{item.status.value}"
        if item.gap_score <= 0:
            return "hunger_gap_satisfied"

        return _CandidateFeature(
            feature=feature,
            item=item,
            phase=phase,
            base_index=base_index,
        )

    @staticmethod
    def _dependencies_by_feature(
        features: list[MissionFeature],
    ) -> dict[str, set[str]]:
        feature_by_id = {feature.feature_id: feature for feature in features}
        dependencies: dict[str, set[str]] = {
            feature.feature_id: set() for feature in features
        }
        for feature in features:
            for possible_dependency in features:
                dependency_id = possible_dependency.feature_id
                if dependency_id == feature.feature_id:
                    continue
                if MissionPlanner._feature_references_dependency(
                    feature,
                    possible_dependency,
                ):
                    dependencies[feature.feature_id].add(dependency_id)

        return {
            feature_id: {
                dependency_id
                for dependency_id in dependency_ids
                if dependency_id in feature_by_id
            }
            for feature_id, dependency_ids in dependencies.items()
        }

    @staticmethod
    def _feature_references_dependency(
        feature: MissionFeature,
        possible_dependency: MissionFeature,
    ) -> bool:
        dependency_markers = (
            possible_dependency.feature_id,
            possible_dependency.hunger_item_id,
        )
        for precondition in feature.preconditions:
            if any(marker in precondition for marker in dependency_markers):
                return True
        for verification_step in feature.verification_steps:
            if any(marker in verification_step for marker in dependency_markers):
                return True
        return False

    @staticmethod
    def _raise_for_cycles(
        dependencies_by_feature: dict[str, set[str]],
        ordered_feature_ids: list[str],
    ) -> None:
        order_index = {
            feature_id: index for index, feature_id in enumerate(ordered_feature_ids)
        }
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []
        stack_index: dict[str, int] = {}

        def visit(feature_id: str) -> list[str] | None:
            visiting.add(feature_id)
            stack_index[feature_id] = len(stack)
            stack.append(feature_id)

            for dependency_id in sorted(
                dependencies_by_feature[feature_id],
                key=lambda value: order_index[value],
            ):
                if dependency_id in visited:
                    continue
                if dependency_id in visiting:
                    return stack[stack_index[dependency_id] :] + [dependency_id]
                cycle = visit(dependency_id)
                if cycle is not None:
                    return cycle

            visiting.remove(feature_id)
            visited.add(feature_id)
            stack.pop()
            stack_index.pop(feature_id)
            return None

        for feature_id in ordered_feature_ids:
            if feature_id in visited:
                continue
            cycle = visit(feature_id)
            if cycle is not None:
                raise PlannerCycleError(cycle)

    @staticmethod
    def _topological_order(
        selected_feature_ids: list[str],
        dependencies_by_feature: dict[str, set[str]],
    ) -> list[str]:
        selected_set = set(selected_feature_ids)
        base_order = {
            feature_id: index for index, feature_id in enumerate(selected_feature_ids)
        }
        indegree = {
            feature_id: len(dependencies_by_feature[feature_id] & selected_set)
            for feature_id in selected_feature_ids
        }
        dependents: dict[str, list[str]] = {
            feature_id: [] for feature_id in selected_feature_ids
        }
        for feature_id in selected_feature_ids:
            for dependency_id in dependencies_by_feature[feature_id] & selected_set:
                dependents[dependency_id].append(feature_id)

        ready = sorted(
            [feature_id for feature_id, count in indegree.items() if count == 0],
            key=lambda value: base_order[value],
        )
        ordered: list[str] = []
        while ready:
            feature_id = ready.pop(0)
            ordered.append(feature_id)
            for dependent_id in sorted(
                dependents[feature_id],
                key=lambda value: base_order[value],
            ):
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    ready.append(dependent_id)
                    ready.sort(key=lambda value: base_order[value])

        if len(ordered) != len(selected_feature_ids):
            raise PlannerCycleError(selected_feature_ids)
        return ordered

    @staticmethod
    def _select_feature_ids(
        sorted_candidates: list[_CandidateFeature],
        dependencies_by_feature: dict[str, set[str]],
        limit: int,
    ) -> list[str]:
        selected: list[str] = []
        selected_set: set[str] = set()
        sorted_feature_ids = [
            candidate.feature.feature_id for candidate in sorted_candidates
        ]

        for candidate in sorted_candidates:
            if len(selected) >= limit:
                break
            feature_id = candidate.feature.feature_id
            if feature_id in selected_set:
                continue
            closure = MissionPlanner._dependency_closure(
                feature_id,
                sorted_feature_ids,
                dependencies_by_feature,
            )
            missing = [
                closure_feature_id
                for closure_feature_id in closure
                if closure_feature_id not in selected_set
            ]
            slots_remaining = limit - len(selected)
            for closure_feature_id in missing[:slots_remaining]:
                selected.append(closure_feature_id)
                selected_set.add(closure_feature_id)

        return selected

    @staticmethod
    def _dependency_closure(
        feature_id: str,
        ordered_feature_ids: list[str],
        dependencies_by_feature: dict[str, set[str]],
    ) -> list[str]:
        closure: set[str] = set()

        def collect(current_feature_id: str) -> None:
            if current_feature_id in closure:
                return
            for dependency_id in dependencies_by_feature[current_feature_id]:
                collect(dependency_id)
            closure.add(current_feature_id)

        collect(feature_id)
        closure_in_base_order = [
            candidate_feature_id
            for candidate_feature_id in ordered_feature_ids
            if candidate_feature_id in closure
        ]
        return MissionPlanner._topological_order(
            closure_in_base_order,
            dependencies_by_feature,
        )

    @staticmethod
    def _assignment_for_candidate(
        *,
        task_id: str,
        loop_id: int,
        index: int,
        mission: Mission,
        candidate: _CandidateFeature,
        dependencies_by_feature: dict[str, set[str]],
        assignment_id_by_feature_id: dict[str, str],
        selected_feature_ids: set[str],
        budget: BudgetAllocation,
    ) -> Assignment:
        feature_id = candidate.feature.feature_id
        selected_dependencies = dependencies_by_feature[feature_id] & selected_feature_ids
        depends_on = [
            assignment_id_by_feature_id[dependency_id]
            for dependency_id in selected_dependencies
        ]
        depends_on.sort()
        return Assignment(
            assignment_id=f"ASGN-{task_id}-{loop_id}-{index}",
            agent_id=EXECUTION_WORKER_V1.agent_id,
            mission=MissionPlanner._mission_for(candidate, mission),
            target_hunger_item_ids=[candidate.feature.hunger_item_id],
            target_feature_ids=[feature_id],
            allowed_tools=list(EXECUTION_WORKER_V1.allowed_tools),
            depends_on=depends_on,
            role="executor",
            max_retries=budget.max_assignment_retries,
        )

    @staticmethod
    def _mission_for(candidate: _CandidateFeature, mission: Mission) -> str:
        feature = candidate.feature
        item = candidate.item
        return (
            f"Mission {mission.mission_id}: {mission.title}\n"
            f"{mission.description}\n\n"
            f"Feature {feature.feature_id}: {feature.title}\n"
            f"{feature.description}\n"
            f"Hunger item: {feature.hunger_item_id} "
            f"(priority={item.priority} gap_score={item.gap_score} "
            f"refinement_tier={item.refinement_tier})"
        )

    @staticmethod
    def _rationale(
        *,
        mission: Mission,
        selected_feature_ids: set[str],
        skip_reasons: dict[str, str],
        item_by_id: dict[str, HungerItem],
    ) -> str:
        lines: list[str] = []
        if not selected_feature_ids:
            lines.append(_NO_CANDIDATES_RATIONALE)

        for feature in mission.features:
            item = item_by_id.get(feature.hunger_item_id)
            metrics = MissionPlanner._metrics_for(item)
            if feature.feature_id in selected_feature_ids:
                lines.append(f"selected {feature.feature_id}: {metrics}")
            else:
                reason = skip_reasons.get(feature.feature_id, "not_selected")
                lines.append(f"skipped {feature.feature_id}: {reason}; {metrics}")
        return "\n".join(lines)

    @staticmethod
    def _metrics_for(item: HungerItem | None) -> str:
        if item is None:
            return "priority=unknown gap_score=unknown refinement_tier=unknown"
        return (
            f"priority={item.priority} "
            f"gap_score={item.gap_score} "
            f"refinement_tier={item.refinement_tier}"
        )
