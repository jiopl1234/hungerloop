"""Cold-start draft sampling gating and selection (v0.7.2)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.planning import Assignment, BudgetAllocation, LoopPlan
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.agent_registry import AgentSpecRegistry
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.loop_orchestrator import (
    LoopOrchestrator,
    _should_draft_sample,
)
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validation_pipeline import ValidationPipelineResult
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager


def test_draft_sampling_requires_cold_start() -> None:
    base = {
        "draft_sampling_k": 3,
        "has_assignments": True,
        "accepted_check_keys": [],
        "prior_trace_count": 0,
    }
    assert _should_draft_sample(**base) is True
    assert _should_draft_sample(**{**base, "draft_sampling_k": 1}) is False
    assert _should_draft_sample(**{**base, "has_assignments": False}) is False
    assert _should_draft_sample(**{**base, "accepted_check_keys": ["H-1:0"]}) is False
    assert _should_draft_sample(**{**base, "prior_trace_count": 2}) is False


# ---- orchestrator-level draft sampling ----
#
# Construction below mirrors ``_build_orchestrator`` in
# tests/unit/test_loop_orchestrator.py (its DI wiring), with an empty
# WorkerRuntime worker map since ``_run_assignments`` is monkeypatched.


def _make_orchestrator(
    tmp_path: Path,
) -> tuple[LoopOrchestrator, InMemoryRepository, WorkspaceManager]:
    repo = InMemoryRepository()
    AgentSpecRegistry().register_defaults(repo)

    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace("t1")

    sandbox = SandboxRunner(repo)
    cost_guard = CostGuard(repo)
    budget_guard = BudgetGuard()
    runtime = WorkerRuntime({}, cost_guard, budget_guard, repo)

    orchestrator = LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(),
        workspace_manager=workspace_manager,
        budget_allocator=BudgetAllocator(),
        planner=RuleBasedPlanner(repo),
        context_builder=ContextBuilder(repo, workspace_reader=workspace_manager),
        worker_runtime=runtime,
        integrator=Integrator(),
        validation_gate=ValidationGate(
            repo, AcceptanceCheckRunner(repo, workspace_manager, sandbox)
        ),
        commit_manager=CommitManager(repo, workspace_manager),
        handoff_processor=HandoffProcessor(repo),
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(repo),
        refinement_compiler=RefinementCompiler(repo),
        max_loops_safety_cap=20,
    )
    return orchestrator, repo, workspace_manager


def _make_scheduler_result() -> Any:
    from hungerloop.services.worker_scheduler import SchedulerResult

    handoff = WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="t1",
        loop_id=1,
        summary="draft",
        evidence_ids=["ev"],
    )
    return SchedulerResult(handoffs=[handoff], skipped_ids=[])


def _plan_with_one_assignment(task_id: str, loop_id: int) -> LoopPlan:
    return LoopPlan(
        task_id=task_id,
        loop_id=loop_id,
        selected_hunger_item_ids=["H-1"],
        assignments=[
            Assignment(
                assignment_id=f"ASGN-{task_id}-{loop_id}-0",
                agent_id="execution_worker_v1",
                mission="produce a.py",
                target_hunger_item_ids=["H-1"],
            )
        ],
    )


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


def _report(loop_id: int, *, newly_passed: list[str]) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-draft-{loop_id}",
        task_id="t1",
        loop_id=loop_id,
        candidate_state_id=f"CAND-t1-{loop_id}",
        baseline_state_id=None,
        verdict=ValidationVerdict.PASS,
        attempted_hunger_item_ids=["H-1"],
        evidence_ids=["ev-pipeline"],
        newly_passed_check_keys=newly_passed,
        currently_passed_check_keys=newly_passed,
        satisfied_hunger_item_ids=["H-1"] if newly_passed else [],
        has_real_progress=bool(newly_passed),
    )


def _pipeline_result(report: ValidationReport) -> ValidationPipelineResult:
    return ValidationPipelineResult(
        deterministic_report=report,
        scrutiny_report=None,
        user_testing_report=None,
        pipeline_verdict="pass" if report.newly_passed_check_keys else "fail",
        stages_run=["deterministic"],
    )


async def test_draft_sampling_selects_best_draft(
    monkeypatch: Any, tmp_path: Path
) -> None:
    orchestrator, repo, workspace_manager = _make_orchestrator(tmp_path)
    task_id, loop_id = "t1", 1
    workspace_manager.create_candidate_workspace(task_id, loop_id)
    files = workspace_manager.candidate_files_dir(task_id, loop_id)

    draft_outputs = iter(["draft one", "draft two", "draft three"])

    async def fake_run_assignments(**kwargs: Any) -> Any:
        (files / "a.py").write_text(next(draft_outputs), encoding="utf-8")
        return _make_scheduler_result()

    reports = iter(
        [
            _report(loop_id, newly_passed=[]),  # draft 1: nothing
            _report(loop_id, newly_passed=["H-1:0"]),  # draft 2: winner
            _report(loop_id, newly_passed=[]),  # draft 3: nothing
        ]
    )

    async def fake_pipeline_run(**kwargs: Any) -> ValidationPipelineResult:
        return _pipeline_result(next(reports))

    monkeypatch.setattr(orchestrator, "_run_assignments", fake_run_assignments)
    monkeypatch.setattr(orchestrator.validation_pipeline, "run", fake_pipeline_run)

    result = await orchestrator._run_draft_sampling(
        task_id=task_id,
        loop_id=loop_id,
        plan=_plan_with_one_assignment(task_id, loop_id),
        budget=_budget(),
        draft_k=3,
        mission=None,
    )
    assert result is not None
    assert (files / "a.py").read_text(encoding="utf-8") == "draft two"
    events = [e for e in repo.list_events(task_id) if e["event_type"] == "draft_sampled"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["winner_draft_index"] == 2
    assert payload["winner_passes_gate"] is True
    assert payload["requested_k"] == 3
    assert payload["draft_count"] == 3
    assert len(payload["per_draft"]) == 3


async def test_draft_sampling_short_circuits_on_identical_content(
    monkeypatch: Any, tmp_path: Path
) -> None:
    orchestrator, repo, workspace_manager = _make_orchestrator(tmp_path)
    task_id, loop_id = "t1", 1
    workspace_manager.create_candidate_workspace(task_id, loop_id)
    files = workspace_manager.candidate_files_dir(task_id, loop_id)

    async def fake_run_assignments(**kwargs: Any) -> Any:
        # Identical bytes every draft — a deterministic provider.
        (files / "a.py").write_text("same content", encoding="utf-8")
        return _make_scheduler_result()

    async def fake_pipeline_run(**kwargs: Any) -> ValidationPipelineResult:
        return _pipeline_result(_report(loop_id, newly_passed=["H-1:0"]))

    monkeypatch.setattr(orchestrator, "_run_assignments", fake_run_assignments)
    monkeypatch.setattr(orchestrator.validation_pipeline, "run", fake_pipeline_run)

    result = await orchestrator._run_draft_sampling(
        task_id=task_id,
        loop_id=loop_id,
        plan=_plan_with_one_assignment(task_id, loop_id),
        budget=_budget(),
        draft_k=3,
        mission=None,
    )
    assert result is not None
    assert (files / "a.py").read_text(encoding="utf-8") == "same content"
    events = [e for e in repo.list_events(task_id) if e["event_type"] == "draft_sampled"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["requested_k"] == 3
    assert payload["draft_count"] == 1
    assert payload["winner_draft_index"] == 1
