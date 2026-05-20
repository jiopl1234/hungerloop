from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import Assignment, BudgetAllocation
from hungerloop.models.worker import AgentSpec, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.worker_scheduler import WorkerScheduler
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
LOOP_ID = 1
AGENT_ID = "execution_worker_v1"


class _RecordingCostGuard:
    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder
        self.calls: list[str] = []

    def assert_within_budget(self, task_id: str) -> None:
        self.calls.append(task_id)
        self.recorder.append("budget")


class _Runtime:
    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerHandoff:
        self.recorder.append(f"run:{context.mission}")
        return WorkerHandoff(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=f"completed {context.mission}",
        )


@pytest.fixture
def repo() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.create_task(TASK_ID, "Goal")
    repository.save_agent_spec(AgentSpec(agent_id=AGENT_ID, name="ExecutionWorker"))
    return repository


def _assignment(assignment_id: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        agent_id=AGENT_ID,
        mission=assignment_id,
        target_hunger_item_ids=[f"H-{assignment_id}"],
        allowed_tools=[],
    )


def _context(assignment: Assignment) -> ContextPack:
    return ContextPack(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        agent_id=assignment.agent_id,
        mission=assignment.assignment_id,
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=list(assignment.target_hunger_item_ids),
        candidate_workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )


async def test_scheduler_pre_post_calls(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    recorder: list[str] = []
    cost_guard = _RecordingCostGuard(recorder)
    runtime = _Runtime(recorder)
    scheduler = WorkerScheduler(
        repo=repo,
        worker_runtime=runtime,
        cost_guard=cost_guard,
        workspace_manager=WorkspaceManager(tmp_path),
    )

    await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID,
        [_assignment("A"), _assignment("B")],
        _context,
    )

    assert cost_guard.calls == [TASK_ID, TASK_ID, TASK_ID, TASK_ID]
    assert recorder == ["budget", "run:A", "budget", "budget", "run:B", "budget"]
