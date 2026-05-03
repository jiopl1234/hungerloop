"""LoopOrchestrator — the v0.5a control loop (PRD §12).

The Orchestrator is the only object that knows the *order* of operations
in one loop iteration. It composes everything from days 1-5:

* :class:`HungerEngine` — phase + stop-reason tick.
* :class:`WorkspaceManager` — candidate / best directory rotation (I-4).
* :class:`BudgetAllocator` — phase → :class:`BudgetAllocation`.
* :class:`RuleBasedPlanner` — pick the next active hunger item (PRD §5).
* :class:`ContextBuilder` — :class:`ContextPack` for each assignment.
* :class:`WorkerRuntime` — dispatch worker, map errors.
* :class:`Integrator` → :class:`ValidationGate` → :class:`CommitManager`
  — promote/reject the candidate (I-3, I-5).
* :class:`HungerUpdateService` + :class:`StagnationDetector` —
  per-item state transitions and global no-progress streak.

:meth:`step` returns either a :class:`LoopTrace` (continue) or a
:class:`StopReport` (terminal). :meth:`run` drives :meth:`step` until a
:class:`StopReport` is returned, with a defensive safety cap so a
mis-configured run cannot loop forever in tests.

Memory / skill managers are intentionally :data:`None`-able (Day 11/12);
the orchestrator skips those steps when they are not provided.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import StopReason
from hungerloop.models.hunger import HungerSnapshot
from hungerloop.models.planning import BudgetAllocation, LoopPlan
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import WorkerResult
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import SafetyStopError
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.stop_report_builder import build_stop_report
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager


@runtime_checkable
class _ProposesMemory(Protocol):
    """Optional :class:`MemoryManager` shape (Day 11)."""

    def propose_from_loop(
        self, task_id: str, loop_id: int, validation: ValidationReport
    ) -> object: ...


def _build_delta_summary(
    *, committed: bool, newly_passed: list[str], regressed: list[str], reason: str
) -> str:
    """One-line per-loop summary for traces / CLI output."""
    if committed:
        return f"committed; +{len(newly_passed)} new checks"
    if regressed:
        return f"rejected ({reason}); regressed {len(regressed)}"
    return f"rejected ({reason})"


class LoopOrchestrator:
    """Drive one task through repeated loop iterations."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        hunger_engine: HungerEngine,
        workspace_manager: WorkspaceManager,
        budget_allocator: BudgetAllocator,
        planner: RuleBasedPlanner,
        context_builder: ContextBuilder,
        worker_runtime: WorkerRuntime,
        integrator: Integrator,
        validation_gate: ValidationGate,
        commit_manager: CommitManager,
        hunger_update: HungerUpdateService,
        stagnation_detector: StagnationDetector,
        memory_manager: _ProposesMemory | None = None,
        max_loops_safety_cap: int = 1000,
    ) -> None:
        self.repo = repo
        self.hunger_engine = hunger_engine
        self.workspace_manager = workspace_manager
        self.budget_allocator = budget_allocator
        self.planner = planner
        self.context_builder = context_builder
        self.worker_runtime = worker_runtime
        self.integrator = integrator
        self.validation_gate = validation_gate
        self.commit_manager = commit_manager
        self.hunger_update = hunger_update
        self.stagnation_detector = stagnation_detector
        self.memory_manager = memory_manager
        self.max_loops_safety_cap = max_loops_safety_cap

    async def step(self, task_id: str) -> LoopTrace | StopReport:
        """Execute one loop iteration (PRD §12.2).

        Returns a :class:`LoopTrace` to continue or a :class:`StopReport`
        to terminate. The Orchestrator never raises; cost ceilings hit
        inside :class:`WorkerRuntime` are converted to ``SAFETY_STOP``
        reports here.
        """
        policy = self.repo.get_hunger_policy(task_id)
        clock = self.repo.get_hunger_clock(task_id)
        ledger = self.repo.get_hunger_ledger(task_id)
        previous_phase = self.repo.get_last_phase(task_id)

        snapshot = self.hunger_engine.tick(
            policy, clock, ledger, previous_phase=previous_phase
        )
        self.repo.save_hunger_snapshot(task_id, snapshot)

        if snapshot.should_stop:
            stop_reason = snapshot.stop_reason or StopReason.ERROR
            return self._emit_stop(task_id, stop_reason)

        loop_id = self.repo.next_loop_id(task_id)

        # Consume one loop budget unit as soon as the loop is accepted.
        clock.loop_count += 1
        self.repo.save_hunger_clock(clock)

        candidate_root = self.workspace_manager.create_candidate_workspace(
            task_id, loop_id
        )
        # Copy: InMemoryRepository returns the live counter object, so without
        # an immutable snapshot here the "delta this loop" math collapses to 0
        # once tools start writing evidence.
        usage_before = self.repo.get_usage_snapshot(task_id).model_copy()

        budget = self.budget_allocator.allocate(snapshot)
        plan = self.planner.plan(task_id, loop_id, snapshot, budget)
        self.repo.save_loop_plan(plan)

        if not plan.assignments:
            return self._handle_empty_plan(
                task_id=task_id,
                loop_id=loop_id,
                snapshot=snapshot,
                plan=plan,
            )

        try:
            worker_results = await self._run_assignments(
                task_id=task_id,
                loop_id=loop_id,
                plan=plan,
                budget=budget,
                candidate_root=candidate_root,
            )
        except SafetyStopError:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            return self._emit_stop(task_id, StopReason.SAFETY_STOP)

        if any(r.requires_human for r in worker_results):
            self.workspace_manager.reject_candidate(task_id, loop_id)
            return self._emit_stop(task_id, StopReason.HUMAN_REQUIRED)

        candidate = self.integrator.integrate(task_id, loop_id, worker_results)
        self.repo.save_candidate(candidate)

        validation = await self.validation_gate.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            target_hunger_item_ids=plan.selected_hunger_item_ids,
        )
        self.repo.save_validation_report(validation)

        commit_decision = self.commit_manager.apply(candidate, validation)
        self.hunger_update.apply_validation(task_id, validation)
        stagnation = self.stagnation_detector.update(task_id, loop_id, validation)

        if self.memory_manager is not None:
            self.memory_manager.propose_from_loop(task_id, loop_id, validation)
        # SkillCard generation is end-of-task only (PRD §20.2); the CLI calls
        # SkillManager.maybe_create_skill_card after the StopReport is built.

        usage_after = self.repo.get_usage_snapshot(task_id).model_copy()
        trace = LoopTrace(
            task_id=task_id,
            loop_id=loop_id,
            phase=budget.phase.value,
            active_hunger=snapshot.active_hunger,
            drive_budget=snapshot.drive_budget,
            work_pressure=snapshot.work_pressure,
            selected_hunger_item_ids=plan.selected_hunger_item_ids,
            worker_ids=[a.agent_id for a in plan.assignments],
            candidate_state_id=candidate.id,
            validation_report_id=validation.id,
            committed=commit_decision["committed"],
            newly_passed_check_keys=list(validation.newly_passed_check_keys),
            regressed_check_keys=list(validation.regressed_check_keys),
            blocked_items_added=list(stagnation["blocked_items"]),
            blocked_item_ids=list(stagnation["blocked_items"]),
            tokens_consumed_this_loop=usage_after.tokens - usage_before.tokens,
            cost_this_loop_usd=usage_after.cost_usd - usage_before.cost_usd,
            llm_calls=usage_after.llm_calls - usage_before.llm_calls,
            tool_calls=usage_after.tool_calls - usage_before.tool_calls,
            delta_summary=_build_delta_summary(
                committed=commit_decision["committed"],
                newly_passed=list(validation.newly_passed_check_keys),
                regressed=list(validation.regressed_check_keys),
                reason=commit_decision["reason"],
            ),
            next_action="continue",
        )
        self.repo.save_loop_trace(trace)

        if stagnation["global_blocked"]:
            return self._emit_stop(task_id, StopReason.BLOCKED)
        return trace

    async def run(self, task_id: str) -> StopReport:
        """Drive :meth:`step` until a :class:`StopReport` is returned.

        The safety cap protects mis-configured tests / demos from
        spinning indefinitely; in normal operation hunger expiration or
        global stagnation terminates the loop long before the cap.
        """
        for _ in range(self.max_loops_safety_cap):
            outcome = await self.step(task_id)
            if isinstance(outcome, StopReport):
                return outcome
        return self._emit_stop(
            task_id,
            StopReason.ERROR,
            recommendation=(
                f"max_loops_safety_cap={self.max_loops_safety_cap} reached; "
                "investigate why hunger never expired"
            ),
        )

    async def _run_assignments(
        self,
        *,
        task_id: str,
        loop_id: int,
        plan: LoopPlan,
        budget: BudgetAllocation,
        candidate_root: Path,
    ) -> list[WorkerResult]:
        """Build context and dispatch each assignment in plan order."""
        results: list[WorkerResult] = []
        for assignment in plan.assignments:
            spec = self.repo.get_agent_spec(assignment.agent_id)
            context: ContextPack = self.context_builder.build_for_agent(
                task_id=task_id,
                loop_id=loop_id,
                agent_id=assignment.agent_id,
                mission=assignment.mission,
                target_hunger_item_ids=assignment.target_hunger_item_ids,
                budget=budget,
                allowed_tools=assignment.allowed_tools,
                output_schema_name=spec.output_schema_name,
                candidate_workspace_ref=f"candidates/loop_{loop_id:03d}",
            )
            result = await self.worker_runtime.run(
                spec, context, workspace_root=candidate_root
            )
            self.repo.save_worker_result(result)
            results.append(result)
        return results

    def _handle_empty_plan(
        self,
        *,
        task_id: str,
        loop_id: int,
        snapshot: HungerSnapshot,
        plan: LoopPlan,
    ) -> LoopTrace | StopReport:
        """No assignments — bump the streak; only stop when the threshold trips."""
        self.workspace_manager.reject_candidate(task_id, loop_id)
        streak = self.repo.increment_no_progress_streak(task_id)

        trace = LoopTrace(
            task_id=task_id,
            loop_id=loop_id,
            phase=plan.phase.value,
            active_hunger=snapshot.active_hunger,
            drive_budget=snapshot.drive_budget,
            work_pressure=snapshot.work_pressure,
            selected_hunger_item_ids=[],
            worker_ids=[],
            candidate_state_id=None,
            validation_report_id=None,
            committed=False,
            delta_summary="empty plan",
            next_action="continue",
        )
        self.repo.save_loop_trace(trace)

        if streak >= self.stagnation_detector.max_global_no_progress:
            return self._emit_stop(task_id, StopReason.BLOCKED)
        return trace

    def _emit_stop(
        self,
        task_id: str,
        stop_reason: StopReason,
        *,
        recommendation: str = "",
    ) -> StopReport:
        """Build and return a :class:`StopReport`.

        Persistence is the CLI's responsibility (PRD §28.16 / M4): the
        Orchestrator never calls ``repo.save_stop_report``. Tests that
        need a record in repository state must call ``save_stop_report``
        themselves after receiving the report.
        """
        return build_stop_report(
            self.repo,
            task_id,
            stop_reason,
            recommendation=recommendation,
        )


