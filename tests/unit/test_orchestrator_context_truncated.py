"""LoopOrchestrator context_truncated event tests."""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.context import ContextPack, TruncationInfo
from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.events import EventType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.agent_registry import AgentSpecRegistry
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.loop_orchestrator import LoopOrchestrator
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager


class _PackBuilder:
    def __init__(self, pack: ContextPack) -> None:
        self.pack = pack

    def build_for_agent(self, **kwargs: object) -> ContextPack:
        return self.pack


def _seed(repo: InMemoryRepository) -> None:
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(max_total_cost_usd=10.0, max_total_tokens=100_000),
    )
    repo.get_hunger_clock("t1")
    item = HungerItem(
        id="H-001",
        title="deliverable",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "missing.txt"},
                description="missing.txt exists",
            )
        ],
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)


def _orchestrator(
    tmp_path: Path,
    repo: InMemoryRepository,
    pack: ContextPack,
) -> LoopOrchestrator:
    AgentSpecRegistry().register_defaults(repo)
    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace("t1")
    sandbox = SandboxRunner(repo)
    budget_guard = BudgetGuard()
    return LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(),
        workspace_manager=workspace_manager,
        budget_allocator=BudgetAllocator(),
        planner=RuleBasedPlanner(repo),
        context_builder=_PackBuilder(pack),  # type: ignore[arg-type]
        worker_runtime=WorkerRuntime({}, CostGuard(repo), budget_guard, repo),
        integrator=Integrator(),
        validation_gate=ValidationGate(
            repo,
            AcceptanceCheckRunner(repo, workspace_manager, sandbox),
        ),
        commit_manager=CommitManager(repo, workspace_manager),
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(repo),
        max_loops_safety_cap=1,
    )


def _pack(truncated: bool) -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="m",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="candidates/loop_001",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
        truncation_info=TruncationInfo(chars_before=2100, chars_after=1900)
        if truncated
        else None,
    )


async def test_context_truncated_precedes_worker_started(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed(repo)

    await _orchestrator(tmp_path, repo, _pack(truncated=True)).step("t1")

    loop_events = [
        event["event_type"]
        for event in repo.list_events("t1", since_loop=1, until_loop=1)
    ]
    assert EventType.CONTEXT_TRUNCATED.value in loop_events
    assert (
        loop_events.index(EventType.CONTEXT_TRUNCATED.value)
        < loop_events.index(EventType.WORKER_STARTED.value)
    )


async def test_no_context_truncated_when_pack_not_truncated(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed(repo)

    await _orchestrator(tmp_path, repo, _pack(truncated=False)).step("t1")

    loop_events = {
        event["event_type"]
        for event in repo.list_events("t1", since_loop=1, until_loop=1)
    }
    assert EventType.CONTEXT_TRUNCATED.value not in loop_events
    assert EventType.WORKER_STARTED.value in loop_events
