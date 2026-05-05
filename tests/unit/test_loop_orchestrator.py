"""End-to-end tests for LoopOrchestrator (PRD §12)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    StopReason,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.worker import WorkerResult
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.agent_registry import AgentSpecRegistry
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.execution_worker import ExecutionWorker
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.loop_orchestrator import LoopOrchestrator
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.tool_harness import ToolHarness
from hungerloop.services.tools import default_tool_registry
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.worker_runtime import Worker, WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager


def _seed_task(repo: InMemoryRepository, items: list[HungerItem]) -> None:
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.get_hunger_clock("t1")  # lazily create
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))
    for item in items:
        repo.save_hunger_item(item)


def _build_orchestrator(
    *,
    tmp_path: Path,
    repo: InMemoryRepository,
    workers: dict[str, Worker],
    max_global_no_progress: int = 5,
) -> LoopOrchestrator:
    AgentSpecRegistry().register_defaults(repo)

    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace("t1")

    sandbox = SandboxRunner(repo)
    cost_guard = CostGuard(repo)
    budget_guard = BudgetGuard()
    runtime = WorkerRuntime(workers, cost_guard, budget_guard, repo)

    return LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(),
        workspace_manager=workspace_manager,
        budget_allocator=BudgetAllocator(),
        planner=RuleBasedPlanner(repo),
        context_builder=ContextBuilder(repo),
        worker_runtime=runtime,
        integrator=Integrator(),
        validation_gate=ValidationGate(
            repo, AcceptanceCheckRunner(repo, workspace_manager, sandbox)
        ),
        commit_manager=CommitManager(repo, workspace_manager),
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(
            repo, max_global_no_progress_loops=max_global_no_progress
        ),
        max_loops_safety_cap=20,
    )


def _build_full_orchestrator(
    *,
    tmp_path: Path,
    repo: InMemoryRepository,
    model_client: DummyModelClient,
) -> LoopOrchestrator:
    AgentSpecRegistry().register_defaults(repo)
    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace("t1")

    sandbox = SandboxRunner(repo)
    cost_guard = CostGuard(repo)
    budget_guard = BudgetGuard()
    harness = ToolHarness(repo, default_tool_registry(sandbox), budget_guard)
    worker = ExecutionWorker(model_client, harness, repo)
    runtime = WorkerRuntime(
        {"execution_worker_v1": worker}, cost_guard, budget_guard, repo
    )

    return LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(),
        workspace_manager=workspace_manager,
        budget_allocator=BudgetAllocator(),
        planner=RuleBasedPlanner(repo),
        context_builder=ContextBuilder(repo),
        worker_runtime=runtime,
        integrator=Integrator(),
        validation_gate=ValidationGate(
            repo, AcceptanceCheckRunner(repo, workspace_manager, sandbox)
        ),
        commit_manager=CommitManager(repo, workspace_manager),
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(repo),
        max_loops_safety_cap=10,
    )


# ---- happy-path end-to-end ----


async def test_run_completes_when_action_satisfies_check(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    item = HungerItem(
        id="H-001",
        title="produce report",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
                description="Report file exists",
            ),
        ],
    )
    _seed_task(repo, [item])

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo report\n"},
        }
    ]
    orchestrator = _build_full_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        model_client=DummyModelClient.with_actions(actions),
    )

    report = await orchestrator.run("t1")
    assert isinstance(report, StopReport)
    assert report.stop_reason is StopReason.DONE
    assert report.goal_status == "completed"
    assert report.accepted_check_keys_count == 1


async def test_step_returns_loop_trace_when_progress_made(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    item = HungerItem(
        id="H-001",
        title="produce report",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
            ),
        ],
    )
    _seed_task(repo, [item])
    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "ok"},
        }
    ]
    orchestrator = _build_full_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        model_client=DummyModelClient.with_actions(actions),
    )

    outcome = await orchestrator.step("t1")
    assert isinstance(outcome, LoopTrace)
    assert outcome.committed is True
    assert outcome.newly_passed_check_keys == ["H-001:0"]
    assert outcome.tool_calls >= 1
    event_types = [event["event_type"] for event in repo._events]
    assert "loop_started" in event_types
    assert "loop_committed" in event_types


# ---- empty plan -> BLOCKED via stagnation streak ----


async def test_empty_plan_streak_triggers_blocked(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    blocked = HungerItem(
        id="H-001",
        title="stuck",
        status=HungerItemStatus.OPEN,
        priority=1.0,
        gap_score=0.0,  # not active (gap_score > 0 required)
    )
    _seed_task(repo, [blocked])

    orchestrator = _build_full_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        model_client=DummyModelClient(),
    )
    # The seed item has gap_score=0 so ledger.is_done() is True -> DONE.
    # Replace with a no-active scenario differently: an item that is BLOCKED.
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-001",
                    title="stuck but unfinished",
                    status=HungerItemStatus.OPEN,
                    priority=1.0,
                    gap_score=1.0,
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.FILE_EXISTS,
                            params={"path": "never.md"},
                        )
                    ],
                ),
            ],
        ),
    )
    # Force planner to return empty by using a low max_global_no_progress
    # and patching the ledger between steps. Simpler: register a stagnation
    # threshold of 1 and rebuild orchestrator with it.
    orchestrator2 = _build_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        workers={},  # no workers — but plan still routes to execution_worker_v1
        max_global_no_progress=1,
    )

    # Trigger an empty-plan path by removing the active item right before step.
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    # An empty ledger means is_done() is True so HungerEngine returns DONE.
    # To actually exercise empty-plan -> BLOCKED, mark the only item BLOCKED
    # so it's unfinished AND inactive. Then HungerEngine returns BLOCKED at
    # the snapshot stage (all_remaining_items_blocked is True). This means
    # the empty-plan branch is reached only when items have gap_score>0 yet
    # are PAUSED. Use that.
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-001",
                    title="paused",
                    status=HungerItemStatus.PAUSED,
                    priority=1.0,
                    gap_score=1.0,
                ),
            ],
        ),
    )

    outcome = await orchestrator2.step("t1")
    # PAUSED in unfinished_items but excluded from active_items -> empty plan
    # path. With max_global_no_progress=1, first empty-plan loop trips BLOCKED.
    assert isinstance(outcome, StopReport)
    assert outcome.stop_reason is StopReason.BLOCKED
    assert outcome.goal_status == "blocked"
    assert any(event["event_type"] == "loop_rejected" for event in repo._events)
    del orchestrator  # unused


# ---- worker error paths ----


class _RaisingWorker:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult:
        raise self._exc


class _RequiresHumanWorker:
    """Worker that simulates the WorkerRuntime auth_error path directly."""

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult:
        return WorkerResult(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error="auth_error: dummy",
            error_type="auth_error",
            requires_human=True,
            retryable=False,
        )


async def test_safety_stop_propagates(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    item = HungerItem(
        id="H-001",
        title="x",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "x.md"},
            )
        ],
    )
    _seed_task(repo, [item])

    orchestrator = _build_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        workers={
            "execution_worker_v1": _RaisingWorker(SafetyStopError("ceiling"))
        },
    )

    outcome = await orchestrator.step("t1")
    assert isinstance(outcome, StopReport)
    assert outcome.stop_reason is StopReason.SAFETY_STOP
    assert outcome.goal_status == "abandoned"
    assert any(event["event_type"] == "safety_stop" for event in repo._events)


async def test_requires_human_short_circuits(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    item = HungerItem(
        id="H-001",
        title="x",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "x.md"},
            )
        ],
    )
    _seed_task(repo, [item])

    orchestrator = _build_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        workers={"execution_worker_v1": _RequiresHumanWorker()},
    )

    outcome = await orchestrator.step("t1")
    assert isinstance(outcome, StopReport)
    assert outcome.stop_reason is StopReason.HUMAN_REQUIRED
    assert outcome.goal_status == "paused"
    assert any(event["event_type"] == "human_required" for event in repo._events)


async def test_human_paused_via_frozen_clock(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    item = HungerItem(id="H-001", title="x", priority=1.0, gap_score=1.0)
    _seed_task(repo, [item])
    clock = repo.get_hunger_clock("t1")
    clock.frozen = True
    repo.save_hunger_clock(clock)

    orchestrator = _build_orchestrator(
        tmp_path=tmp_path, repo=repo, workers={}
    )
    outcome = await orchestrator.step("t1")
    assert isinstance(outcome, StopReport)
    assert outcome.stop_reason is StopReason.HUMAN_PAUSED
    assert outcome.goal_status == "paused"


async def test_safety_cap_emits_error_after_excessive_loops(tmp_path: Path) -> None:
    """Defensive cap: if hunger never expires, run() returns ERROR."""
    repo = InMemoryRepository()
    # Always-active item with a check that fails forever (file never created).
    item = HungerItem(
        id="H-001",
        title="forever",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "missing.md"},
            )
        ],
    )
    _seed_task(repo, [item])
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=1e9,
            max_total_tokens=10**12,
            initial_hunger=1e9,
            decay_duration_seconds=1e9,
        ),
    )
    orchestrator = _build_full_orchestrator(
        tmp_path=tmp_path,
        repo=repo,
        # Empty dummy: every loop returns "no actions" so nothing satisfies
        # H-001; stagnation will eventually fire BLOCKED long before the cap.
        model_client=DummyModelClient(),
    )
    # Explicitly use a very low cap to make sure the test runs fast even if
    # stagnation logic somehow let it through.
    orchestrator.max_loops_safety_cap = 3
    orchestrator.stagnation_detector.max_global_no_progress = 100  # disable

    outcome = await orchestrator.run("t1")
    assert isinstance(outcome, StopReport)
    assert outcome.stop_reason is StopReason.ERROR
    assert "max_loops_safety_cap=3" in outcome.recommendation

