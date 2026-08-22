"""Cold-start draft sampling gating and selection (v0.7.2)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.planning import Assignment, BudgetAllocation, LoopPlan
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.agent_registry import AgentSpecRegistry
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
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


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_delete_evidence_preserves_actual_tool_call_usage(
    backend: str, tmp_path: Path
) -> None:
    """Context cleanup must not erase tool calls that actually ran."""
    repo: Any = (
        InMemoryRepository()
        if backend == "memory"
        else SQLiteRepository(tmp_path / "t.sqlite")
    )
    repo.create_task("t1", "goal")
    keep = repo.save_tool_call_as_evidence(
        task_id="t1", loop_id=1, agent_id="a", tool_name="write_file",
        args_summary="{}", result_summary="ok", success=True, elapsed_ms=1,
    )
    drop = repo.save_tool_call_as_evidence(
        task_id="t1", loop_id=1, agent_id="a", tool_name="patch_file",
        args_summary="{}", result_summary="no", success=False, elapsed_ms=1,
    )
    assert repo.get_usage_snapshot("t1").tool_calls == 2

    repo.delete_evidence([drop])

    usage = repo.get_usage_snapshot("t1")
    assert usage.tool_calls == 2
    assert repo.aggregate_evidence_usage("t1").tool_calls == 1
    assert {row["evidence_id"] for row in repo.list_evidence("t1")} == {keep}
    if backend == "sqlite":
        repo.close()


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


# Deterministic per-loop handoff primary key, exactly as
# worker_scheduler._persist_handoff builds it (retry_count=0):
# WH-<task>-<loop>-<assignment_id>.
_HANDOFF_ID = "WH-t1-1-ASGN-t1-1-0"


def _persist_draft_handoff(repo: InMemoryRepository, summary: str) -> Any:
    """Mirror worker_scheduler._persist_handoff: persist a handoff carrying
    the deterministic per-loop id so the draft primary-key collision class
    is exercised (each draft re-runs under the same loop_id)."""
    from hungerloop.services.worker_scheduler import SchedulerResult

    handoff = WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="t1",
        loop_id=1,
        summary=summary,
        evidence_ids=["ev"],
        handoff_id=_HANDOFF_ID,
    )
    repo.save_worker_handoff(handoff)
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
        summary = next(draft_outputs)
        (files / "a.py").write_text(summary, encoding="utf-8")
        return _persist_draft_handoff(repo, summary)

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
    assert payload["worker_passes_run"] == 3
    assert payload["short_circuited_draft_indexes"] == []
    assert payload["short_circuited_count"] == 0
    assert len(payload["per_draft"]) == 3
    # Important defect: the persisted handoff rows for the loop must hold
    # exactly the WINNER's handoff (draft two), never the last draft's
    # (draft three) — otherwise a discarded loser would feed next-loop
    # planning. The collision-safe delete keeps the row set clean too.
    handoffs = repo.list_worker_handoffs(task_id)
    assert [handoff.summary for handoff in handoffs] == ["draft two"]


async def test_draft_sampling_runs_all_identical_drafts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    orchestrator, repo, workspace_manager = _make_orchestrator(tmp_path)
    task_id, loop_id = "t1", 1
    workspace_manager.create_candidate_workspace(task_id, loop_id)
    files = workspace_manager.candidate_files_dir(task_id, loop_id)

    async def fake_run_assignments(**kwargs: Any) -> Any:
        # Identical bytes every draft — a deterministic provider.
        (files / "a.py").write_text("same content", encoding="utf-8")
        return _persist_draft_handoff(repo, "same content")

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
    assert payload["draft_count"] == 3
    assert payload["worker_passes_run"] == 3
    assert payload["short_circuited_draft_indexes"] == []
    assert payload["short_circuited_count"] == 0
    assert payload["winner_draft_index"] == 1
    # Exactly one persisted handoff row survives (the winner's), despite the
    # per-draft re-persist under the same deterministic id.
    assert len(repo.list_worker_handoffs(task_id)) == 1


def _tool_call_evidence_ids(repo: InMemoryRepository, task_id: str) -> set[str]:
    ids = {
        str(row["evidence_id"])
        for row in repo.list_successful_tool_call_evidence(task_id)
    }
    ids.update(
        str(row["evidence_id"])
        for row in repo.list_failed_tool_call_evidence(task_id)
    )
    return ids


async def test_draft_sampling_deletes_loser_tool_call_evidence(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Only the WINNER draft's tool-call evidence survives a sampling run.

    Each draft's worker pass persists tool-call evidence under the same
    task/loop/agent (mirroring ToolHarness). If the losers' rows were kept
    they would render as phantom-state evidence lines / inflated
    ``failure_patterns_to_avoid`` in the winner's next-loop ContextBuilder
    history.
    """
    orchestrator, repo, workspace_manager = _make_orchestrator(tmp_path)
    task_id, loop_id = "t1", 1
    workspace_manager.create_candidate_workspace(task_id, loop_id)
    files = workspace_manager.candidate_files_dir(task_id, loop_id)

    draft_outputs = iter(["draft one", "draft two", "draft three"])
    # summary -> (successful_evidence_id, failed_evidence_id)
    draft_evidence: dict[str, tuple[str, str]] = {}

    async def fake_run_assignments(**kwargs: Any) -> Any:
        summary = next(draft_outputs)
        (files / "a.py").write_text(summary, encoding="utf-8")
        ok = repo.save_tool_call_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            agent_id="execution_worker_v1",
            tool_name="write_file",
            args_summary="path=a.py",
            result_summary=summary,
            success=True,
            elapsed_ms=1,
        )
        bad = repo.save_tool_call_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            agent_id="execution_worker_v1",
            tool_name="patch_file",
            args_summary=f"path=a.py want={summary}",
            result_summary="no matching lines",
            success=False,
            elapsed_ms=1,
        )
        draft_evidence[summary] = (ok, bad)
        return _persist_draft_handoff(repo, summary)

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

    await orchestrator._run_draft_sampling(
        task_id=task_id,
        loop_id=loop_id,
        plan=_plan_with_one_assignment(task_id, loop_id),
        budget=_budget(),
        draft_k=3,
        mission=None,
    )

    winner_ids = set(draft_evidence["draft two"])
    surviving = _tool_call_evidence_ids(repo, task_id)
    # Surviving tool-call evidence is EXACTLY the winner's rows.
    assert surviving == winner_ids
    # Losers' rows (drafts one + three) are deleted outright.
    loser_ids = set(draft_evidence["draft one"]) | set(draft_evidence["draft three"])
    assert surviving.isdisjoint(loser_ids)
    # Winner's rows are untouched (same ids, content preserved).
    winner_ok, _winner_bad = draft_evidence["draft two"]
    successful = {
        str(row["evidence_id"]): row
        for row in repo.list_successful_tool_call_evidence(task_id)
    }
    assert winner_ok in successful
    payload = successful[winner_ok]["payload"]
    assert isinstance(payload, dict)
    assert payload["result_summary"] == "draft two"
    # Audit: 2 losers x 2 rows each were deleted.
    events = [
        e for e in repo.list_events(task_id) if e["event_type"] == "draft_sampled"
    ]
    assert events[0]["payload"]["loser_evidence_rows_deleted"] == 4  # type: ignore[index]


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def test_delete_evidence_removes_named_rows(backend: str, tmp_path: Path) -> None:
    """Repo-level: delete_evidence drops only the named ids; unknown ids no-op."""
    repo: Any
    if backend == "in_memory":
        repo = InMemoryRepository()
    else:
        repo = SQLiteRepository.open(tmp_path / "evidence.sqlite")
    repo.create_task("t1", "goal")
    keep = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        tool_name="write_file",
        args_summary="path=a.py",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    drop = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        tool_name="patch_file",
        args_summary="path=a.py",
        result_summary="no match",
        success=False,
        elapsed_ms=1,
    )

    # Unknown ids are ignored, named ids removed.
    repo.delete_evidence([drop, "ev-does-not-exist"])

    remaining = {str(row["evidence_id"]) for row in repo.list_evidence("t1")}
    assert remaining == {keep}

    if isinstance(repo, SQLiteRepository):
        repo.close()


async def test_draft_sampling_abort_cleans_loser_state_on_safety_stop(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """SafetyStopError mid-sampling must not leak loser evidence/handoffs.

    Regression guard: before the fix, an aborted draft loop left the
    in-flight draft's handoff rows and every completed loser's tool-call
    evidence behind, so a resumed loop's planning context read phantom
    state from the aborted sampling.
    """
    orchestrator, repo, workspace_manager = _make_orchestrator(tmp_path)
    task_id, loop_id = "t1", 1
    workspace_manager.create_candidate_workspace(task_id, loop_id)
    files = workspace_manager.candidate_files_dir(task_id, loop_id)

    draft_outputs = iter(["draft one", "draft two"])

    async def fake_run_assignments(**kwargs: Any) -> Any:
        summary = next(draft_outputs)
        (files / "a.py").write_text(summary, encoding="utf-8")
        repo.save_tool_call_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            agent_id="execution_worker_v1",
            tool_name="write_file",
            args_summary="a.py",
            result_summary=summary,
            success=True,
            elapsed_ms=1,
        )
        return _persist_draft_handoff(repo, summary)

    pipeline_calls = 0

    async def fake_pipeline_run(**kwargs: Any) -> ValidationPipelineResult:
        nonlocal pipeline_calls
        pipeline_calls += 1
        if pipeline_calls == 1:
            return _pipeline_result(_report(loop_id, newly_passed=[]))
        raise SafetyStopError("cost ceiling exceeded")

    monkeypatch.setattr(orchestrator, "_run_assignments", fake_run_assignments)
    monkeypatch.setattr(orchestrator.validation_pipeline, "run", fake_pipeline_run)

    with pytest.raises(SafetyStopError):
        await orchestrator._run_draft_sampling(
            task_id=task_id,
            loop_id=loop_id,
            plan=_plan_with_one_assignment(task_id, loop_id),
            budget=_budget(),
            draft_k=3,
            mission=None,
        )

    # Draft 1 completed (evidence + handoff), draft 2 was in flight when the
    # stop hit: the abort cleanup must remove both drafts' loop-scoped state.
    assert pipeline_calls == 2
    assert repo.list_worker_handoffs(task_id) == []
    assert repo.list_failed_tool_call_evidence(task_id) == []
    assert repo.list_successful_tool_call_evidence(task_id) == []
