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

from typing import Protocol, runtime_checkable

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import CompletionMode, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerClockState, HungerLedger, HungerPolicy, HungerSnapshot
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import Assignment, BudgetAllocation, LoopPlan
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import WorkerHandoff, WorkerResult
from hungerloop.repository.migration_errors import IllegalPhaseTransition
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import SafetyStopError
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.mission_planner import MissionPlanner, PlannerCycleError
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.stop_report_builder import build_stop_report
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validation_pipeline import ValidationPipeline
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.worker_scheduler import SchedulerResult, WorkerScheduler
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
        mission_planner: MissionPlanner | None = None,
        worker_scheduler: WorkerScheduler | None = None,
        context_builder: ContextBuilder,
        worker_runtime: WorkerRuntime,
        integrator: Integrator,
        validation_gate: ValidationGate,
        commit_manager: CommitManager,
        handoff_processor: HandoffProcessor,
        hunger_update: HungerUpdateService,
        stagnation_detector: StagnationDetector,
        refinement_compiler: RefinementCompiler,
        validation_pipeline: ValidationPipeline | None = None,
        memory_manager: _ProposesMemory | None = None,
        max_loops_safety_cap: int = 1000,
    ) -> None:
        self.repo = repo
        self.hunger_engine = hunger_engine
        if self.hunger_engine.repo is None:
            self.hunger_engine.repo = repo
        self.workspace_manager = workspace_manager
        self.budget_allocator = budget_allocator
        self.planner = planner
        self.mission_planner = mission_planner or MissionPlanner(repo, planner)
        self.worker_scheduler = worker_scheduler or WorkerScheduler(
            repo=repo,
            worker_runtime=worker_runtime,
            cost_guard=worker_runtime.cost_guard,
            workspace_manager=workspace_manager,
        )
        self.context_builder = context_builder
        self.worker_runtime = worker_runtime
        self.integrator = integrator
        self.validation_gate = validation_gate
        self.validation_pipeline = (
            validation_pipeline
            or ValidationPipeline.from_validation_gate(
                repo=repo,
                cost_guard=worker_runtime.cost_guard,
                validation_gate=validation_gate,
            )
        )
        self.commit_manager = commit_manager
        self.handoff_processor = handoff_processor
        self.hunger_update = hunger_update
        self.stagnation_detector = stagnation_detector
        self.refinement_compiler = refinement_compiler
        self.memory_manager = memory_manager
        self.max_loops_safety_cap = max_loops_safety_cap
        # Set inside ``_step_inner`` immediately after ``next_loop_id`` is
        # allocated so ``_emit_error_stop`` can persist the ERROR LoopTrace
        # under the same loop_id the §7.5 events fired under (post-review I1).
        self._current_loop_id: int | None = None

    async def step(self, task_id: str) -> LoopTrace | StopReport:
        """Execute one loop iteration (PRD §12.2).

        Returns a :class:`LoopTrace` to continue or a :class:`StopReport`
        to terminate. The Orchestrator never raises; cost ceilings hit
        inside :class:`WorkerRuntime` are converted to ``SAFETY_STOP``
        reports here.

        v0.5d.0 (PRD §7.5): emits the fine-grained lifecycle event
        sequence — LOOP_STARTED → LOOP_PLANNED → (WORKER_STARTED →
        … → WORKER_FINISHED|FAILED)+ → CANDIDATE_CREATED →
        VALIDATION_STARTED → CHECK_*+ → VALIDATION_FINISHED →
        CANDIDATE_COMMITTED|REJECTED → LOOP_COMMITTED|LOOP_REJECTED.
        Synthetic worker exceptions are caught at this level so a
        :class:`LoopTrace` and ERROR :class:`StopReport` always
        persist (FR-9 / synthetic-worker-exception persistence).
        """
        self._current_loop_id = None
        try:
            return await self._step_inner(task_id)
        except (SafetyStopError, KeyboardInterrupt):
            # SafetyStop has its own emit path inside _step_inner; KeyboardInterrupt
            # is operator-driven and not orchestrator-error.
            raise
        except Exception as exc:  # pragma: no cover - exercised by D0-13 test
            return self._emit_error_stop(
                task_id, exc, loop_id=self._current_loop_id
            )

    async def _step_inner(self, task_id: str) -> LoopTrace | StopReport:
        policy = self.repo.get_hunger_policy(task_id)
        clock = self.repo.get_hunger_clock(task_id)
        ledger = self.repo.get_hunger_ledger(task_id)
        previous_phase = self.repo.get_last_phase(task_id)

        snapshot = self.hunger_engine.tick(
            policy,
            clock,
            ledger,
            previous_phase=previous_phase,
            task_id=task_id,
        )

        budgeted = self._maybe_expand_or_stop_budgeted_refinement(
            task_id=task_id,
            policy=policy,
            clock=clock,
            ledger=ledger,
            previous_phase=previous_phase,
            snapshot=snapshot,
        )
        if isinstance(budgeted, StopReport):
            # The refinement hook decides BUDGET_EXHAUSTED outside the
            # engine, so the snapshot's stop_reason still reads DONE.
            # Persist a snapshot whose stop_reason matches the actual
            # StopReport so audit views (hunger_snapshots stream,
            # repair-state diagnostics) don't disagree with the
            # StopReport's terminal verdict.
            final_snapshot = snapshot.model_copy(
                update={"stop_reason": budgeted.stop_reason}
            )
            self.repo.save_hunger_snapshot(task_id, final_snapshot)
            return budgeted
        if isinstance(budgeted, tuple):
            ledger, snapshot = budgeted

        self.repo.save_hunger_snapshot(task_id, snapshot)

        if snapshot.should_stop:
            stop_reason = snapshot.stop_reason or StopReason.ERROR
            return self._emit_stop(task_id, stop_reason)

        loop_id = self.repo.next_loop_id(task_id)
        self._current_loop_id = loop_id

        # Consume one loop budget unit as soon as the loop is accepted.
        clock.loop_count += 1
        self.repo.save_hunger_clock(clock)

        # Capture best_state anchor BEFORE any candidate work so the trace's
        # "before" pointer is unambiguous even on failure paths.
        best_before = self.repo.get_best_state(task_id)
        best_state_id_before = best_before.state_id if best_before else None

        self.workspace_manager.create_candidate_workspace(task_id, loop_id)
        self.repo.append_event(
            EventType.LOOP_STARTED,
            {
                "loop_id": loop_id,
                "phase": snapshot.phase.value,
                "drive_budget": snapshot.drive_budget,
                "active_hunger": snapshot.active_hunger,
            },
            task_id=task_id,
            loop_id=loop_id,
        )
        # Copy: InMemoryRepository returns the live counter object, so without
        # an immutable snapshot here the "delta this loop" math collapses to 0
        # once tools start writing evidence.
        usage_before = self.repo.get_usage_snapshot(task_id).model_copy()

        budget = self.budget_allocator.allocate(snapshot)
        mission = self.repo.get_mission(task_id)
        previous_loop_id = loop_id - 1
        prior_handoffs = self.repo.list_worker_handoffs(
            task_id,
            since_loop_id=previous_loop_id,
        )
        prior_handoffs = [
            handoff for handoff in prior_handoffs if handoff.loop_id == previous_loop_id
        ]
        try:
            if mission is not None:
                plan = self.mission_planner.plan(
                    task_id,
                    loop_id,
                    snapshot,
                    budget,
                    mission=mission,
                    prior_handoffs=prior_handoffs,
                )
            else:
                plan = self.planner.plan(task_id, loop_id, snapshot, budget)
        except PlannerCycleError as exc:
            safety_snapshot = snapshot.model_copy(
                update={"should_stop": True, "stop_reason": StopReason.SAFETY_STOP}
            )
            self.repo.save_hunger_snapshot(task_id, safety_snapshot)
            self.workspace_manager.reject_candidate(task_id, loop_id)
            self.repo.append_event(
                EventType.PLANNER_CYCLE_DETECTED,
                {"loop_id": loop_id, "cycle": list(exc.cycle), "error": str(exc)},
                task_id=task_id,
                loop_id=loop_id,
            )
            self.repo.append_event(
                EventType.SAFETY_STOP,
                {"loop_id": loop_id, "reason": "planner_cycle_detected"},
                task_id=task_id,
                loop_id=loop_id,
            )
            return self._emit_stop(task_id, StopReason.SAFETY_STOP)
        self.repo.save_loop_plan(plan)
        # PRD §7.5: LOOP_PLANNED fires once after the planner returns,
        # regardless of whether the plan has assignments.
        self.repo.append_event(
            EventType.LOOP_PLANNED,
            {
                "loop_id": loop_id,
                "selected_hunger_item_ids": list(plan.selected_hunger_item_ids),
                "worker_ids": [a.agent_id for a in plan.assignments],
                "assignment_count": len(plan.assignments),
            },
            task_id=task_id,
            loop_id=loop_id,
        )

        if not plan.assignments:
            return self._handle_empty_plan(
                task_id=task_id,
                loop_id=loop_id,
                snapshot=snapshot,
                plan=plan,
                best_state_id_before=best_state_id_before,
            )

        try:
            scheduler_result = await self._run_assignments(
                task_id=task_id,
                loop_id=loop_id,
                plan=plan,
                budget=budget,
            )
        except SafetyStopError:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            self.repo.append_event(
                EventType.SAFETY_STOP,
                {"loop_id": loop_id},
                task_id=task_id,
                loop_id=loop_id,
            )
            return self._emit_stop(task_id, StopReason.SAFETY_STOP)

        worker_handoffs, handoff_payloads = self._save_and_emit_handoffs(
            task_id=task_id,
            loop_id=loop_id,
            plan=plan,
            worker_handoffs=scheduler_result.handoffs,
            mission=mission,
        )
        self.handoff_processor.process_handoffs(
            task_id,
            loop_id,
            worker_handoffs,
            mission=mission,
            budget=budget,
        )
        self.repo.append_event(
            EventType.WORKER_HANDOFF_RECEIVED,
            self._handoff_received_payload(
                mission=mission,
                emitted_payloads=handoff_payloads,
            ),
            task_id=task_id,
            loop_id=loop_id,
        )

        if any(handoff.requires_human for handoff in worker_handoffs):
            self.workspace_manager.reject_candidate(task_id, loop_id)
            self.repo.append_event(
                EventType.HUMAN_REQUIRED,
                {
                    "loop_id": loop_id,
                    "agent_ids": [
                        handoff.agent_id
                        for handoff in worker_handoffs
                        if handoff.requires_human
                    ],
                },
                task_id=task_id,
                loop_id=loop_id,
            )
            return self._emit_stop(task_id, StopReason.HUMAN_REQUIRED)

        worker_results = [handoff.as_worker_result() for handoff in worker_handoffs]
        candidate = self.integrator.integrate(task_id, loop_id, worker_results)
        self.repo.save_candidate(candidate)
        self.repo.append_event(
            EventType.CANDIDATE_CREATED,
            {
                "candidate_state_id": candidate.id,
                "loop_id": loop_id,
                "worker_ids": [r.agent_id for r in worker_results],
            },
            task_id=task_id,
            loop_id=loop_id,
        )

        attempted_hunger_item_ids = self._attempted_hunger_item_ids(
            plan,
            skipped_ids=scheduler_result.skipped_ids,
        )
        self.repo.append_event(
            EventType.VALIDATION_STARTED,
            {
                "candidate_state_id": candidate.id,
                "target_hunger_item_ids": attempted_hunger_item_ids,
            },
            task_id=task_id,
            loop_id=loop_id,
        )
        validation_phase = self._phase_for_validation(mission, plan)
        try:
            pipeline_result = await self.validation_pipeline.run(
                task_id=task_id,
                loop_id=loop_id,
                candidate=candidate,
                target_hunger_item_ids=attempted_hunger_item_ids,
                mission=mission,
                phase=validation_phase,
                budget=budget,
            )
        except SafetyStopError:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            self.repo.append_event(
                EventType.SAFETY_STOP,
                {"loop_id": loop_id, "stage": "validation_pipeline"},
                task_id=task_id,
                loop_id=loop_id,
            )
            return self._emit_stop(task_id, StopReason.SAFETY_STOP)
        validation = pipeline_result.deterministic_report
        self.repo.save_validation_report(validation)
        self._emit_check_events(task_id, loop_id, validation)
        self.repo.append_event(
            EventType.VALIDATION_FINISHED,
            {
                "validation_report_id": validation.id,
                "verdict": validation.verdict.value,
                "newly_passed": list(validation.newly_passed_check_keys),
                "regressed": list(validation.regressed_check_keys),
            },
            task_id=task_id,
            loop_id=loop_id,
        )

        commit_decision = self.commit_manager.apply(candidate, pipeline_result)
        commit_verdict = commit_decision["verdict"]
        effective_validation = validation
        effective_pipeline_result = pipeline_result
        if commit_verdict != validation.verdict:
            effective_validation = validation.model_copy(
                update={
                    "verdict": commit_verdict,
                    "newly_passed_check_keys": [],
                    "satisfied_hunger_item_ids": [],
                    "has_real_progress": False,
                }
            )
            effective_pipeline_result = pipeline_result.model_copy(
                update={
                    "deterministic_report": effective_validation,
                    "pipeline_verdict": "fail",
                }
            )
        if commit_decision["committed"]:
            self.repo.append_event(
                EventType.CANDIDATE_COMMITTED,
                {
                    "candidate_state_id": candidate.id,
                    "validation_report_id": effective_validation.id,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        else:
            self.repo.append_event(
                EventType.CANDIDATE_REJECTED,
                {
                    "candidate_state_id": candidate.id,
                    "validation_report_id": effective_validation.id,
                    "reason": commit_decision["reason"],
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        self.hunger_update.apply_validation(task_id, effective_validation)
        stagnation = self.stagnation_detector.update(
            task_id,
            loop_id,
            effective_validation,
            attempted_hunger_item_ids=attempted_hunger_item_ids,
            respect_stagnation=policy.respect_stagnation,
        )

        if self.memory_manager is not None:
            self.memory_manager.propose_from_loop(task_id, loop_id, effective_validation)
        # SkillCard generation is end-of-task only (PRD §20.2); the CLI calls
        # SkillManager.maybe_create_skill_card after the StopReport is built.

        self.hunger_engine.tick(
            policy,
            self.repo.get_hunger_clock(task_id),
            self.repo.get_hunger_ledger(task_id),
            previous_phase=previous_phase,
            task_id=task_id,
            validation_result=effective_pipeline_result,
            validation_phase_id=(
                validation_phase.phase_id if validation_phase is not None else None
            ),
        )

        # best_state may have been promoted by CommitManager.apply.
        best_after = self.repo.get_best_state(task_id)
        best_state_id_after = best_after.state_id if best_after else None

        usage_after = self.repo.get_usage_snapshot(task_id).model_copy()
        trace = LoopTrace(
            task_id=task_id,
            loop_id=loop_id,
            phase=budget.phase.value,
            active_hunger=snapshot.active_hunger,
            drive_budget=snapshot.drive_budget,
            work_pressure=snapshot.work_pressure,
            selected_hunger_item_ids=attempted_hunger_item_ids,
            worker_ids=[a.agent_id for a in plan.assignments],
            candidate_state_id=candidate.id,
            validation_report_id=effective_validation.id,
            committed=commit_decision["committed"],
            newly_passed_check_keys=list(effective_validation.newly_passed_check_keys),
            regressed_check_keys=list(effective_validation.regressed_check_keys),
            currently_passed_check_keys=list(
                effective_validation.currently_passed_check_keys
            ),
            satisfied_hunger_item_ids=list(effective_validation.satisfied_hunger_item_ids),
            unsatisfied_hunger_item_ids=list(
                effective_validation.unsatisfied_hunger_item_ids
            ),
            best_state_id_before_loop=best_state_id_before,
            best_state_id_after_loop=best_state_id_after,
            verdict=effective_validation.verdict.value,
            blocked_items_added=list(stagnation["blocked_items"]),
            blocked_item_ids=list(stagnation["blocked_items"]),
            tokens_consumed_this_loop=usage_after.tokens - usage_before.tokens,
            cost_this_loop_usd=usage_after.cost_usd - usage_before.cost_usd,
            llm_calls=usage_after.llm_calls - usage_before.llm_calls,
            tool_calls=usage_after.tool_calls - usage_before.tool_calls,
            delta_summary=_build_delta_summary(
                committed=commit_decision["committed"],
                newly_passed=list(effective_validation.newly_passed_check_keys),
                regressed=list(effective_validation.regressed_check_keys),
                reason=commit_decision["reason"],
            ),
            next_action="continue",
        )
        self.repo.save_loop_trace(trace)
        if commit_decision["committed"]:
            self.repo.append_event(
                EventType.LOOP_COMMITTED,
                {
                    "candidate_state_id": candidate.id,
                    "validation_report_id": effective_validation.id,
                    "newly_passed_check_keys": list(
                        effective_validation.newly_passed_check_keys
                    ),
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        else:
            self.repo.append_event(
                EventType.LOOP_REJECTED,
                {
                    "candidate_state_id": candidate.id,
                    "validation_report_id": effective_validation.id,
                    "reason": commit_decision["reason"],
                    "regressed_check_keys": list(
                        effective_validation.regressed_check_keys
                    ),
                },
                task_id=task_id,
                loop_id=loop_id,
            )

        if stagnation["global_blocked"]:
            return self._emit_stop(task_id, StopReason.BLOCKED)
        return trace

    def _save_and_emit_handoffs(
        self,
        *,
        task_id: str,
        loop_id: int,
        plan: LoopPlan,
        worker_handoffs: list[WorkerHandoff],
        mission: Mission | None,
    ) -> tuple[list[WorkerHandoff], list[dict[str, object]]]:
        emitted_payloads: list[dict[str, object]] = []
        for index, (assignment, handoff) in enumerate(
            zip(plan.assignments, worker_handoffs, strict=False)
        ):
            handoff_id = (
                handoff.handoff_id
                or f"WH-{task_id}-{loop_id}-{assignment.assignment_id}"
            )
            if handoff.handoff_id is None:
                handoff = handoff.model_copy(update={"handoff_id": handoff_id})
                worker_handoffs[index] = handoff
                self.repo.save_worker_handoff(handoff)
                self.worker_scheduler.persist_handoff_audit(
                    task_id=task_id,
                    loop_id=loop_id,
                    assignment_id=assignment.assignment_id,
                    handoff=handoff,
                )
            payload = self._handoff_event_payload(
                mission=mission,
                assignment=assignment,
                assignment_index=index,
                task_id=task_id,
                loop_id=loop_id,
                handoff=handoff,
                handoff_id=handoff_id,
            )
            self.repo.append_event(
                EventType.WORKER_HANDOFF_EMITTED,
                payload,
                task_id=task_id,
                loop_id=loop_id,
            )
            emitted_payloads.append(payload)
        return worker_handoffs, emitted_payloads

    def _handoff_event_payload(
        self,
        *,
        mission: Mission | None,
        assignment: Assignment,
        assignment_index: int,
        task_id: str,
        loop_id: int,
        handoff: WorkerHandoff,
        handoff_id: str,
    ) -> dict[str, object]:
        feature = self._feature_for_assignment(mission, assignment)
        assignment_id = self._assignment_id(
            assignment,
            task_id=task_id,
            loop_id=loop_id,
            assignment_index=assignment_index,
        )
        return {
            "mission_id": mission.mission_id if mission is not None else None,
            "phase_id": feature.phase_id if feature is not None else None,
            "feature_id": feature.feature_id if feature is not None else None,
            "assignment_id": assignment_id,
            "agent_id": handoff.agent_id,
            "handoff_id": handoff_id,
            "target_hunger_item_ids": list(assignment.target_hunger_item_ids),
        }

    @staticmethod
    def _feature_for_assignment(
        mission: Mission | None,
        assignment: Assignment,
    ) -> MissionFeature | None:
        if mission is None:
            return None
        target_feature_ids = set(getattr(assignment, "target_feature_ids", []))
        target_hunger_item_ids = set(assignment.target_hunger_item_ids)
        for feature in mission.features:
            if (
                feature.feature_id in target_feature_ids
                or feature.hunger_item_id in target_hunger_item_ids
            ):
                return feature
        return None

    @staticmethod
    def _assignment_id(
        assignment: Assignment,
        *,
        task_id: str,
        loop_id: int,
        assignment_index: int,
    ) -> str:
        assignment_id = getattr(assignment, "assignment_id", None)
        if isinstance(assignment_id, str) and assignment_id:
            return assignment_id
        return f"ASGN-{task_id}-{loop_id}-{assignment_index}"

    @staticmethod
    def _handoff_received_payload(
        *,
        mission: Mission | None,
        emitted_payloads: list[dict[str, object]],
    ) -> dict[str, object]:
        phase_ids = LoopOrchestrator._unique_payload_strings(
            emitted_payloads,
            "phase_id",
        )
        feature_ids = LoopOrchestrator._unique_payload_strings(
            emitted_payloads,
            "feature_id",
        )
        assignment_ids = LoopOrchestrator._unique_payload_strings(
            emitted_payloads,
            "assignment_id",
        )
        handoff_ids = LoopOrchestrator._unique_payload_strings(
            emitted_payloads,
            "handoff_id",
        )
        return {
            "mission_id": mission.mission_id if mission is not None else None,
            "phase_id": phase_ids[0] if len(phase_ids) == 1 else None,
            "feature_id": feature_ids[0] if len(feature_ids) == 1 else None,
            "assignment_id": assignment_ids[0] if len(assignment_ids) == 1 else None,
            "phase_ids": phase_ids,
            "feature_ids": feature_ids,
            "assignment_ids": assignment_ids,
            "handoff_ids": handoff_ids,
            "handoff_count": len(emitted_payloads),
        }

    @staticmethod
    def _phase_for_validation(
        mission: Mission | None,
        plan: LoopPlan,
    ) -> MissionPhase | None:
        if mission is None:
            return None
        target_feature_ids: set[str] = set()
        target_hunger_item_ids: set[str] = set()
        for assignment in plan.assignments:
            target_feature_ids.update(assignment.target_feature_ids)
            target_hunger_item_ids.update(assignment.target_hunger_item_ids)
        for feature in mission.features:
            if (
                feature.feature_id in target_feature_ids
                or feature.hunger_item_id in target_hunger_item_ids
            ):
                for phase in mission.phases:
                    if phase.phase_id == feature.phase_id:
                        return phase
        for phase in mission.phases:
            if phase.status == "validating":
                return phase
        return None

    @staticmethod
    def _unique_payload_strings(
        payloads: list[dict[str, object]],
        key: str,
    ) -> list[str]:
        values: list[str] = []
        for payload in payloads:
            value = payload.get(key)
            if isinstance(value, str) and value and value not in values:
                values.append(value)
        return values

    def _maybe_expand_or_stop_budgeted_refinement(
        self,
        *,
        task_id: str,
        policy: HungerPolicy,
        clock: HungerClockState,
        ledger: HungerLedger,
        previous_phase: LoopPhase | None,
        snapshot: HungerSnapshot,
    ) -> tuple[HungerLedger, HungerSnapshot] | StopReport | None:
        """Handle spend-budget refinement before the normal DONE stop fires."""
        if policy.completion_mode != CompletionMode.SPEND_BUDGET:
            return None
        if snapshot.stop_reason != StopReason.DONE:
            return None

        if snapshot.drive_budget <= 0:
            self.repo.append_event(
                EventType.REFINEMENT_BUDGET_EXHAUSTED,
                {
                    "loop_budget_remaining": snapshot.drive_budget,
                    "max_refinement_tier": policy.max_refinement_tier,
                    "profile": policy.refinement_profile,
                },
                task_id=task_id,
            )
            return self._emit_stop(
                task_id,
                StopReason.BUDGET_EXHAUSTED,
                recommendation=(
                    "refinement budget exhausted after tier-0 correctness; "
                    "use --refill to continue refinement or --reset for a new run"
                ),
            )

        result = self.refinement_compiler.ensure_next_tier(
            task_id=task_id,
            policy=policy,
            ledger=ledger,
            best_state=self.repo.get_best_state(task_id),
        )
        if not result.added_item_ids:
            return None
        best_state = self.repo.get_best_state(task_id)
        previous_accepted_check_keys_count = (
            len(best_state.accepted_check_keys) if best_state is not None else 0
        )

        self.repo.append_event(
            EventType.REFINEMENT_TIER_STARTED,
            {
                "tier": result.active_tier,
                "profile": policy.refinement_profile,
                "loop_budget_remaining": snapshot.drive_budget,
            },
            task_id=task_id,
        )
        self.repo.append_event(
            EventType.REFINEMENT_ITEMS_ADDED,
            {
                "tier": result.active_tier,
                "profile": policy.refinement_profile,
                "added_item_ids": list(result.added_item_ids),
                "previous_accepted_check_keys_count": (
                    previous_accepted_check_keys_count
                ),
                "loop_budget_remaining": snapshot.drive_budget,
            },
            task_id=task_id,
        )

        refreshed_ledger = self.repo.get_hunger_ledger(task_id)
        refreshed_snapshot = self.hunger_engine.tick(
            policy,
            clock,
            refreshed_ledger,
            previous_phase=previous_phase,
            task_id=task_id,
        )
        return refreshed_ledger, refreshed_snapshot

    def _emit_check_events(
        self, task_id: str, loop_id: int, validation: ValidationReport
    ) -> None:
        """One CHECK_PASSED / CHECK_FAILED / CHECK_REGRESSED event per row."""
        for check in validation.check_results:
            if check.regressed:
                event_type = EventType.CHECK_REGRESSED
            elif check.passed:
                event_type = EventType.CHECK_PASSED
            else:
                event_type = EventType.CHECK_FAILED
            self.repo.append_event(
                event_type,
                {
                    "check_key": check.check_key,
                    "newly_passed": check.newly_passed,
                },
                task_id=task_id,
                loop_id=loop_id,
            )

    def _emit_error_stop(
        self,
        task_id: str,
        exc: BaseException,
        *,
        loop_id: int | None = None,
    ) -> StopReport:
        """Persist a LoopTrace + StopReport when the orchestrator caught
        an unexpected exception (FR-9 / synthetic-worker-exception path).

        The trace carries ``stop_reason=ERROR`` and a ``worker_errors``
        entry so ``hungerloop trace`` shows the failure cause; the
        StopReport gets the standard resume_hint via _emit_stop.

        ``loop_id`` is the in-flight loop the exception interrupted —
        threaded from ``step()`` via ``self._current_loop_id`` so the
        ERROR LoopTrace and the §7.5 events that already fired under
        that same loop join cleanly. Falls back to a fresh
        ``next_loop_id`` only when the exception fired before any loop
        was allocated (rare; e.g. ``hunger_engine.tick`` blew up).
        """
        # Record the error in the event log so trace export surfaces it.
        self.repo.append_event(
            EventType.ERROR,
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:240],
            },
            task_id=task_id,
        )
        # Best-effort LoopTrace so a downstream `repair-state --check`
        # can detect D10 (stopped task with no trace) cleanly.
        try:
            trace_loop_id = loop_id if loop_id is not None else self.repo.next_loop_id(task_id)
            if isinstance(exc, IllegalPhaseTransition):
                self.repo.append_event(
                    EventType.PHASE_TRANSITION_REJECTED,
                    {
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:240],
                    },
                    task_id=task_id,
                    loop_id=trace_loop_id,
                )
            error_trace = LoopTrace(
                task_id=task_id,
                loop_id=trace_loop_id,
                phase="error",
                active_hunger=0.0,
                drive_budget=0.0,
                work_pressure=0.0,
                committed=False,
                worker_errors=[f"{type(exc).__name__}: {str(exc)[:240]}"],
                stop_reason=StopReason.ERROR,
                delta_summary=f"orchestrator caught {type(exc).__name__}",
                next_action="repair",
            )
            self.repo.save_loop_trace(error_trace)
        except Exception:  # pragma: no cover — defence in depth
            # Don't let trace persistence failure hide the real error.
            pass
        return self._emit_stop(
            task_id,
            StopReason.ERROR,
            recommendation=(
                f"orchestrator caught {type(exc).__name__}: {str(exc)[:240]}; "
                "run repair-state --check, then run --resume or --reset"
            ),
        )

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
    ) -> SchedulerResult:
        """Delegate assignment execution to the M3 WorkerScheduler."""

        def context_factory(assignment: Assignment) -> ContextPack:
            spec = self.repo.get_agent_spec(assignment.agent_id)
            if isinstance(self.context_builder, ContextBuilder):
                return self.context_builder.build_for_agent(
                    assignment,
                    task_id=task_id,
                    loop_id=loop_id,
                    budget=budget,
                    output_schema_name=spec.output_schema_name,
                )
            return self.context_builder.build_for_agent(
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

        return await self.worker_scheduler.execute_assignments(
            task_id,
            loop_id,
            plan.assignments,
            context_factory,
        )

    @staticmethod
    def _attempted_hunger_item_ids(
        plan: LoopPlan,
        *,
        skipped_ids: list[str],
    ) -> list[str]:
        """Return the union of hunger ids for completed, non-skipped assignments."""
        skipped = set(skipped_ids)
        attempted: list[str] = []
        for assignment in plan.assignments:
            if assignment.assignment_id in skipped:
                continue
            for item_id in assignment.target_hunger_item_ids:
                if item_id not in attempted:
                    attempted.append(item_id)
        return attempted

    @staticmethod
    def _worker_failure_message(result: WorkerResult) -> str | None:
        """Extract a short failure description from a WorkerResult.

        Returns ``None`` when the worker is treated as successful (no
        ``error`` field and ``requires_human`` is False); otherwise a
        truncated description suitable for a WORKER_FAILED payload.
        """
        if result.error is not None:
            return result.error[:240]
        if result.requires_human:
            return "requires_human"
        return None

    def _handle_empty_plan(
        self,
        *,
        task_id: str,
        loop_id: int,
        snapshot: HungerSnapshot,
        plan: LoopPlan,
        best_state_id_before: str | None,
    ) -> LoopTrace | StopReport:
        """No assignments — bump the streak; only stop when the threshold trips."""
        self.workspace_manager.reject_candidate(task_id, loop_id)
        streak = self.repo.increment_no_progress_streak(task_id)
        self.repo.append_event(
            EventType.LOOP_REJECTED,
            {"reason": "empty_plan", "streak": streak},
            task_id=task_id,
            loop_id=loop_id,
        )

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
            best_state_id_before_loop=best_state_id_before,
            best_state_id_after_loop=best_state_id_before,
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

        v0.5d.0: emits a ``STOP_REPORT_CREATED`` event so the trace
        log marks every terminal transition. The event is best-effort
        (failures don't block the StopReport return).
        """
        report = build_stop_report(
            self.repo,
            task_id,
            stop_reason,
            recommendation=recommendation,
        )
        try:
            self.repo.append_event(
                EventType.STOP_REPORT_CREATED,
                {
                    "stop_reason": stop_reason.value,
                    "goal_status": report.goal_status,
                    "total_loops": report.total_loops,
                },
                task_id=task_id,
            )
        except Exception:  # pragma: no cover — defensive
            pass
        return report
