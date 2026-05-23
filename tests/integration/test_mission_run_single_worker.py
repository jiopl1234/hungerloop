"""M3 orchestrator wiring integration tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation, LoopPlan
from hungerloop.models.tracing import LoopTrace
from hungerloop.models.worker import HandoffItem, WorkerHandoff
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
from hungerloop.services.loop_orchestrator import LoopOrchestrator
from hungerloop.services.mission_planner import MissionPlanner
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.rule_based_planner import RuleBasedPlanner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.stagnation_detector import StagnationDetector
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.worker_scheduler import WorkerScheduler
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
AGENT_ID = "execution_worker_v1"


def _seed_policy(repo: InMemoryRepository) -> None:
    repo.create_task(TASK_ID, "Mission wiring")
    repo.set_hunger_policy(
        TASK_ID,
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=100_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.get_hunger_clock(TASK_ID)


def _item(item_id: str, path: str) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": path},
                description=f"{path} exists",
            )
        ],
    )


def _mission(feature_ids: list[str], *, max_parallel_features: int = 3) -> Mission:
    features = [
        MissionFeature(
            feature_id=feature_id,
            hunger_item_id=f"H-{feature_id[-1]}",
            phase_id="phase-1",
            title=f"Feature {feature_id}",
            description=f"Implement {feature_id}",
        )
        for feature_id in feature_ids
    ]
    return Mission(
        mission_id="mission-1",
        task_id=TASK_ID,
        title="Mission",
        description="Mission runtime test",
        phases=[
            MissionPhase(
                phase_id="phase-1",
                title="Phase",
                description="Phase one",
                feature_ids=feature_ids,
                status="in_progress",
            )
        ],
        features=features,
        created_at=datetime.now(timezone.utc),
        max_parallel_features=max_parallel_features,
    )


def _save_mission(repo: InMemoryRepository, mission: Mission) -> None:
    repo.save_mission(mission)
    for phase in mission.phases:
        repo.save_mission_phase(phase)
    for feature in mission.features:
        repo.save_mission_feature(feature)


class _RecordingWorker:
    def __init__(self, writes: dict[str, tuple[str, str]]) -> None:
        self.writes = writes
        self.contexts: list[ContextPack] = []

    async def run(self, *, context: ContextPack, workspace_root: Path) -> WorkerHandoff:
        self.contexts.append(context)
        feature_id = context.target_feature_ids[0] if context.target_feature_ids else "legacy"
        path, content = self.writes[feature_id]
        target = workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return WorkerHandoff(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=f"completed {feature_id}",
            artifact_ids=[path],
            evidence_ids=[f"EV-{feature_id}"],
        )


class _RecordingMissionPlanner(MissionPlanner):
    def __init__(self, repo: InMemoryRepository) -> None:
        super().__init__(repo)
        self.calls: list[dict[str, object]] = []

    def plan(
        self,
        task_id: str,
        loop_id: int,
        snapshot: Any,
        budget: BudgetAllocation,
        mission: Mission | None = None,
        prior_handoffs: list[WorkerHandoff] | None = None,
    ) -> LoopPlan:
        self.calls.append(
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "mission": mission,
                "prior_handoffs": list(prior_handoffs or []),
            }
        )
        return super().plan(
            task_id,
            loop_id,
            snapshot,
            budget,
            mission=mission,
            prior_handoffs=prior_handoffs,
        )


class _RecordingRulePlanner(RuleBasedPlanner):
    def __init__(self, repo: InMemoryRepository) -> None:
        super().__init__(repo)
        self.calls = 0

    def plan(
        self,
        task_id: str,
        loop_id: int,
        snapshot: Any,
        budget: BudgetAllocation,
    ) -> LoopPlan:
        self.calls += 1
        return super().plan(task_id, loop_id, snapshot, budget)


class _MaxWorkersBudgetAllocator(BudgetAllocator):
    def __init__(self, max_workers_per_loop: int) -> None:
        self.max_workers_per_loop = max_workers_per_loop

    def allocate(self, snapshot: Any) -> BudgetAllocation:
        return BudgetAllocation(
            phase=snapshot.phase,
            max_workers_per_loop=self.max_workers_per_loop,
        )


def _build_orchestrator(
    *,
    repo: InMemoryRepository,
    tmp_path: Path,
    worker: _RecordingWorker,
    max_workers_per_loop: int,
    mission_planner: MissionPlanner | None = None,
    legacy_planner: RuleBasedPlanner | None = None,
) -> LoopOrchestrator:
    AgentSpecRegistry().register_defaults(repo)
    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace(TASK_ID)
    sandbox = SandboxRunner(repo)
    cost_guard = CostGuard(repo)
    budget_guard = BudgetGuard()
    runtime = WorkerRuntime({AGENT_ID: worker}, cost_guard, budget_guard, repo)
    return LoopOrchestrator(
        repo=repo,
        hunger_engine=HungerEngine(),
        workspace_manager=workspace_manager,
        budget_allocator=_MaxWorkersBudgetAllocator(max_workers_per_loop),
        planner=legacy_planner or RuleBasedPlanner(repo),
        mission_planner=mission_planner,
        worker_scheduler=WorkerScheduler(
            repo=repo,
            worker_runtime=runtime,
            cost_guard=cost_guard,
            workspace_manager=workspace_manager,
        ),
        context_builder=ContextBuilder(repo, workspace_reader=workspace_manager),
        worker_runtime=runtime,
        integrator=Integrator(),
        validation_gate=ValidationGate(
            repo,
            AcceptanceCheckRunner(repo, workspace_manager, sandbox),
        ),
        commit_manager=CommitManager(repo, workspace_manager),
        handoff_processor=HandoffProcessor(repo),
        hunger_update=HungerUpdateService(repo),
        stagnation_detector=StagnationDetector(repo),
        refinement_compiler=RefinementCompiler(repo),
        max_loops_safety_cap=5,
    )


async def test_single_worker_mission_emits_one_assignment(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed_policy(repo)
    repo.save_hunger_ledger(
        TASK_ID,
        HungerLedger(
            task_id=TASK_ID,
            items=[_item("H-1", "one.txt"), _item("H-2", "two.txt")],
        ),
    )
    _save_mission(repo, _mission(["F-1", "F-2"], max_parallel_features=3))
    worker = _RecordingWorker({"F-1": ("one.txt", "one"), "F-2": ("two.txt", "two")})
    orchestrator = _build_orchestrator(
        repo=repo,
        tmp_path=tmp_path,
        worker=worker,
        max_workers_per_loop=1,
    )

    outcome = await orchestrator.step(TASK_ID)

    assert isinstance(outcome, LoopTrace)
    assert len(worker.contexts) == 1
    assert len(repo.list_worker_handoffs(TASK_ID, since_loop_id=1)) == 1
    assert worker.contexts[0].target_feature_ids == ["F-1"]
    assert outcome.mission_snapshot is not None
    assert outcome.mission_snapshot["mission_id"] == "mission-1"
    assert outcome.assignment_traces
    assert outcome.assignment_traces[0]["assignment_id"] == "ASGN-task-1-1-0"
    assert outcome.assignment_traces[0]["target_feature_ids"] == ["F-1"]
    assert outcome.validation_pipeline_trace is not None
    assert outcome.validation_pipeline_trace["pipeline_verdict"] == "pass"


async def test_orchestrator_routes_planner_by_mission_presence(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed_policy(repo)
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[_item("H-1", "one.txt")]))
    prior = WorkerHandoff(
        agent_id=AGENT_ID,
        task_id=TASK_ID,
        loop_id=0,
        handoff_items=[
            HandoffItem(
                item_type="blocker",
                summary="prior blocker for another feature",
                related_feature_ids=["F-other"],
            )
        ],
    )
    repo.save_worker_handoff(prior)
    mission = _mission(["F-1"], max_parallel_features=1)
    _save_mission(repo, mission)
    mission_planner = _RecordingMissionPlanner(repo)
    legacy_planner = _RecordingRulePlanner(repo)
    worker = _RecordingWorker({"F-1": ("one.txt", "one")})
    orchestrator = _build_orchestrator(
        repo=repo,
        tmp_path=tmp_path,
        worker=worker,
        max_workers_per_loop=1,
        mission_planner=mission_planner,
        legacy_planner=legacy_planner,
    )

    outcome = await orchestrator.step(TASK_ID)

    assert isinstance(outcome, LoopTrace)
    assert legacy_planner.calls == 0
    assert len(mission_planner.calls) == 1
    assert mission_planner.calls[0]["mission"] == mission
    prior_handoffs = mission_planner.calls[0]["prior_handoffs"]
    assert isinstance(prior_handoffs, list)
    assert len(prior_handoffs) == 1
    assert isinstance(prior_handoffs[0], WorkerHandoff)
    assert prior_handoffs[0].handoff_items == prior.handoff_items


async def test_legacy_task_uses_rule_based_planner(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed_policy(repo)
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[_item("H-1", "one.txt")]))
    mission_planner = _RecordingMissionPlanner(repo)
    legacy_planner = _RecordingRulePlanner(repo)
    worker = _RecordingWorker({"legacy": ("one.txt", "one")})
    orchestrator = _build_orchestrator(
        repo=repo,
        tmp_path=tmp_path,
        worker=worker,
        max_workers_per_loop=5,
        mission_planner=mission_planner,
        legacy_planner=legacy_planner,
    )

    outcome = await orchestrator.step(TASK_ID)

    assert isinstance(outcome, LoopTrace)
    assert legacy_planner.calls == 1
    assert mission_planner.calls == []
    assert len(worker.contexts) == 1
    assert worker.contexts[0].target_feature_ids == []


async def test_event_order_matches_loop_spec(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed_policy(repo)
    repo.save_hunger_ledger(
        TASK_ID,
        HungerLedger(
            task_id=TASK_ID,
            items=[_item("H-1", "one.txt"), _item("H-2", "two.txt")],
        ),
    )
    _save_mission(repo, _mission(["F-1", "F-2"], max_parallel_features=2))
    worker = _RecordingWorker({"F-1": ("one.txt", "one"), "F-2": ("two.txt", "two")})
    orchestrator = _build_orchestrator(
        repo=repo,
        tmp_path=tmp_path,
        worker=worker,
        max_workers_per_loop=2,
    )

    outcome = await orchestrator.step(TASK_ID)

    assert isinstance(outcome, LoopTrace)
    loop_events = [
        str(event["event_type"])
        for event in repo.list_events(TASK_ID, since_loop=1, until_loop=1)
    ]
    canonical = [
        "loop_started",
        "loop_planned",
        "worker.assignment_started",
        "worker.assignment_completed",
        "worker.assignment_started",
        "worker.assignment_completed",
        "candidate_created",
        "validation_started",
    ]
    cursor = -1
    for event_name in canonical:
        cursor = loop_events.index(event_name, cursor + 1)
    assert loop_events[-1] in {"loop_committed", "loop_rejected"}
