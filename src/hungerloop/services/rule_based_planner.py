"""RuleBasedPlanner for HungerLoop v0.5a.

Deterministic, LLM-free planner (PRD §5). v0.5.2 explicitly excludes
``LLMPlanner``; the Orchestrator calls
``planner.plan(task_id, loop_id, snapshot, budget)`` once per loop and the
planner picks the next active hunger item by ``priority * gap_score``.

Per PRD §5.5 / §28.11, an empty plan is **not** an immediate ``BLOCKED``
signal — the planner returns an empty :class:`LoopPlan` with rationale, and
the Orchestrator drives the no-progress streak.
"""
from __future__ import annotations

from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerItem, HungerSnapshot
from hungerloop.models.planning import (
    Assignment,
    BudgetAllocation,
    LoopPlan,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.agent_registry import EXECUTION_WORKER_V1

_EMPTY_PLAN_RATIONALE = "No active hunger items available for planning."


class RuleBasedPlanner:
    """Deterministic planner that routes the highest-pressure active item to
    ``execution_worker_v1`` (PRD §5.4).

    The planner never raises :class:`StopReason.BLOCKED` directly — it
    delegates to the Orchestrator's no-progress logic (PRD §5.5).
    """

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def plan(
        self,
        task_id: str,
        loop_id: int,
        snapshot: HungerSnapshot,
        budget: BudgetAllocation,
    ) -> LoopPlan:
        """Build a single-assignment :class:`LoopPlan` for the next loop.

        Args:
            task_id: Task identifier.
            loop_id: Current loop counter assigned by the Orchestrator.
            snapshot: Current :class:`HungerSnapshot` (used for mission phase).
            budget: Per-phase :class:`BudgetAllocation`. ``max_workers_per_loop``
                caps concurrency; v0.5a is fixed to 1 (PRD §28.11 / M11).

        Returns:
            ``LoopPlan`` with at most one assignment routed to
            ``execution_worker_v1``. Empty assignments when no active items
            remain (callers must handle empty plans per PRD §5.5).
        """
        ledger = self.repo.get_hunger_ledger(task_id)
        active_items = ledger.active_items()
        lowest_tier = min(
            (item.refinement_tier for item in active_items),
            default=0,
        )
        items = sorted(
            (item for item in active_items if item.refinement_tier == lowest_tier),
            key=lambda item: item.priority * item.gap_score,
            reverse=True,
        )

        n = min(1, budget.max_workers_per_loop)
        selected = items[:n]

        if not selected:
            return LoopPlan(
                task_id=task_id,
                loop_id=loop_id,
                selected_hunger_item_ids=[],
                assignments=[],
                phase=budget.phase,
                rationale=_EMPTY_PLAN_RATIONALE,
            )

        item = selected[0]
        task = self.repo.get_task(task_id)
        raw_goal = task.raw_goal if task is not None else ""
        assignment = Assignment(
            assignment_id=f"ASGN-{task_id}-{loop_id}-0",
            agent_id=EXECUTION_WORKER_V1.agent_id,
            mission=self._mission_for(item, snapshot.phase, raw_goal),
            target_hunger_item_ids=[item.id],
            allowed_tools=list(EXECUTION_WORKER_V1.allowed_tools),
        )
        return LoopPlan(
            task_id=task_id,
            loop_id=loop_id,
            selected_hunger_item_ids=[item.id],
            assignments=[assignment],
            phase=budget.phase,
            rationale=f"Selected highest priority active item: {item.id}",
        )

    @staticmethod
    def _mission_for(item: HungerItem, phase: LoopPhase, raw_goal: str) -> str:
        """Compose a deterministic mission string for the worker.

        Includes the original user goal verbatim so the worker has the task
        semantics, not just the structural acceptance check descriptions.
        """
        if item.acceptance_checks:
            checks_summary = "; ".join(
                check.description or check.check_type.value
                for check in item.acceptance_checks
            )
        else:
            checks_summary = "no acceptance checks"
        goal_block = (
            f"User goal:\n{raw_goal.strip()}\n\n" if raw_goal.strip() else ""
        )
        return (
            f"[phase={phase.value} tier={item.refinement_tier} "
            f"kind={item.refinement_kind}] Make progress on {item.id}: "
            f"{item.title}.\n"
            f"{goal_block}"
            f"Acceptance: {checks_summary}."
        )
