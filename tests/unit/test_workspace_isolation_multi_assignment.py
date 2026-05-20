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


class _NoopCostGuard:
    def assert_within_budget(self, task_id: str) -> None:
        assert task_id == TASK_ID


class _WritingRuntime:
    def __init__(self, repo: InMemoryRepository, writes: dict[str, tuple[str, str]]) -> None:
        self.repo = repo
        self.writes = writes
        self.workspace_roots: list[Path] = []

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerHandoff:
        assert spec.agent_id == context.agent_id
        self.workspace_roots.append(workspace_root)
        path, content = self.writes[context.mission]
        target = workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        evidence_id = self.repo.save_tool_call_as_evidence(
            task_id=context.task_id,
            loop_id=context.loop_id,
            agent_id=context.agent_id,
            tool_name="write_file",
            args_summary=f"path={path} bytes={len(content)}",
            result_summary=f"wrote {len(content)} chars",
            success=True,
            elapsed_ms=1,
        )
        return WorkerHandoff(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=f"wrote {path}",
            evidence_ids=[evidence_id],
        )


@pytest.fixture
def repo() -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.create_task(TASK_ID, "Goal")
    repository.save_agent_spec(
        AgentSpec(agent_id=AGENT_ID, name="ExecutionWorker", allowed_tools=["write_file"])
    )
    return repository


def _assignment(assignment_id: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        agent_id=AGENT_ID,
        mission=assignment_id,
        target_hunger_item_ids=[f"H-{assignment_id}"],
        allowed_tools=["write_file"],
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
        allowed_tools=list(assignment.allowed_tools),
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )


async def test_disjoint_writes_succeed(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(tmp_path)
    runtime = _WritingRuntime(
        repo,
        {
            "A": ("alpha.txt", "alpha"),
            "B": ("nested/beta.txt", "beta"),
        },
    )
    scheduler = WorkerScheduler(
        repo=repo,
        worker_runtime=runtime,
        cost_guard=_NoopCostGuard(),
        workspace_manager=workspace,
    )

    result = await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID,
        [_assignment("A"), _assignment("B")],
        _context,
    )

    candidate_files = workspace.candidate_files_dir(TASK_ID, LOOP_ID)
    assert (candidate_files / "alpha.txt").read_text(encoding="utf-8") == "alpha"
    assert (candidate_files / "nested" / "beta.txt").read_text(encoding="utf-8") == "beta"
    assert not (workspace.task_root(TASK_ID) / "candidates" / "loop_001" / "assignments").exists()
    assert result.skipped_ids == []
    assert repo.list_events(TASK_ID, event_types=["WORKSPACE_WRITE_COLLISION"]) == []
    assert runtime.workspace_roots == [candidate_files, candidate_files]


async def test_collision_emits_event(
    repo: InMemoryRepository,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(tmp_path)
    runtime = _WritingRuntime(
        repo,
        {
            "A": ("conflict.txt", "first"),
            "B": ("conflict.txt", "second"),
        },
    )
    scheduler = WorkerScheduler(
        repo=repo,
        worker_runtime=runtime,
        cost_guard=_NoopCostGuard(),
        workspace_manager=workspace,
    )

    result = await scheduler.execute_assignments(
        TASK_ID,
        LOOP_ID,
        [_assignment("A"), _assignment("B")],
        _context,
    )

    candidate_files = workspace.candidate_files_dir(TASK_ID, LOOP_ID)
    assert (candidate_files / "conflict.txt").read_text(encoding="utf-8") == "second"
    assert [handoff.assignment_id for handoff in result.handoffs] == ["A", "B"]
    collision_events = repo.list_events(TASK_ID, event_types=["WORKSPACE_WRITE_COLLISION"])
    assert len(collision_events) == 1
    assert collision_events[0]["payload"] == {
        "paths": ["conflict.txt"],
        "assignments_by_path": {"conflict.txt": ["A", "B"]},
    }
