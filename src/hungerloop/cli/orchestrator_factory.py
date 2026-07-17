"""Wire a fully-configured :class:`LoopOrchestrator` for the CLI.

The CLI's ``run`` command creates an orchestrator on every invocation;
this factory keeps the wiring out of the click command so tests can
build the same object directly.

v0.5a uses :class:`DummyModelClient` because :class:`OpenAIModelClient`
ships on Day 9. The factory accepts an optional override so demos /
integration tests can inject a scripted dummy.
"""
from __future__ import annotations

from pathlib import Path
from typing import cast

from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.agent_registry import AgentSpecRegistry
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.check_proposal_gate import CheckProposalGate, SandboxDryRunner
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.execution_worker import ExecutionWorker
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.integrator import Integrator
from hungerloop.services.loop_orchestrator import LoopOrchestrator, _SpecSynthesizerProtocol
from hungerloop.services.memory_manager import MemoryManager
from hungerloop.services.model_client import DummyModelClient, ModelClient
from hungerloop.services.refactor_transaction_manager import RefactorTransactionManager
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.tool_harness import ToolHarness
from hungerloop.services.tools import default_tool_registry
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validation_pipeline import ValidationPipeline
from hungerloop.services.validators.scrutiny_validator import ScrutinyValidator
from hungerloop.services.validators.user_testing_validator import UserTestingValidator
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager
from hungerloop.services.workspace_reader import WorkspaceReader


def build_orchestrator(
    *,
    repo: RepositoryProtocol,
    workspace_root: Path,
    model_client: ModelClient | None = None,
    budget_allocator: BudgetAllocator | None = None,
    max_loops_safety_cap: int = 200,
    spec_check_synthesizer: _SpecSynthesizerProtocol | None = None,
    max_global_no_progress_loops: int = 5,
) -> LoopOrchestrator:
    """Wire all v0.5a services into a :class:`LoopOrchestrator`.

    Side effect: ``AgentSpecRegistry().register_defaults(repo)`` is called
    so the planner-emitted assignments resolve through ``repo.get_agent_spec``
    on first use (PRD §6.1).
    """
    AgentSpecRegistry().register_defaults(repo)

    workspace_manager = WorkspaceManager(workspace_root)
    sandbox = SandboxRunner(repo)
    cost_guard = CostGuard(repo)
    budget_guard = BudgetGuard()

    refactor_transaction_manager = RefactorTransactionManager(
        repo=repo, workspace_manager=workspace_manager
    )

    client: ModelClient = model_client if model_client is not None else DummyModelClient()
    harness = ToolHarness(repo, default_tool_registry(sandbox), budget_guard)
    worker = ExecutionWorker(client, harness, repo, budget_guard=budget_guard)

    runtime = WorkerRuntime(
        {"execution_worker_v1": worker}, cost_guard, budget_guard, repo
    )
    handoff_processor = HandoffProcessor(
        repo,
        check_proposal_gate=CheckProposalGate(
            dry_runner=SandboxDryRunner(sandbox),
        ),
        refinement_compiler=RefinementCompiler(repo),
        refactor_transaction_manager=refactor_transaction_manager,
        workspace_manager=workspace_manager,
    )
    validation_gate = ValidationGate(
        repo, AcceptanceCheckRunner(repo, workspace_manager, sandbox)
    )
    validation_pipeline = ValidationPipeline.from_validation_gate(
        repo=repo,
        cost_guard=cost_guard,
        validation_gate=validation_gate,
        scrutiny_validator=ScrutinyValidator(
            repo=repo,
            sandbox_runner=sandbox,
            workspace_manager=workspace_manager,
            handoff_processor=handoff_processor,
        ),
        user_testing_validator=UserTestingValidator(
            repo=repo,
            sandbox_runner=sandbox,
            workspace_manager=workspace_manager,
        ),
    )

    return LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(repo),
        workspace_manager=workspace_manager,
        budget_allocator=budget_allocator or BudgetAllocator(),
        planner=RuleBasedPlanner(repo),
        context_builder=ContextBuilder(
            repo,
            workspace_reader=cast(WorkspaceReader, workspace_manager),
        ),
        worker_runtime=runtime,
        integrator=Integrator(),
        validation_gate=validation_gate,
        validation_pipeline=validation_pipeline,
        commit_manager=CommitManager(repo, workspace_manager),
        handoff_processor=handoff_processor,
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(
            repo, max_global_no_progress_loops=max_global_no_progress_loops
        ),
        refinement_compiler=RefinementCompiler(repo),
        memory_manager=MemoryManager(repo),
        max_loops_safety_cap=max_loops_safety_cap,
        spec_check_synthesizer=spec_check_synthesizer,
        refactor_transaction_manager=refactor_transaction_manager,
    )
