"""Sequential assignment scheduler for v0.6 mission loops."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hungerloop.models.context import ContextPack
from hungerloop.models.events import EventType
from hungerloop.models.planning import Assignment
from hungerloop.models.worker import WorkerHandoff, WorkerResult
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.worker_runtime import WorkerRuntime
from hungerloop.services.workspace_manager import WorkspaceManager

_PATH_RE = re.compile(r"(?:^|\s)path=([^\s]+)")
_WRITE_TOOLS = {"write_file", "patch_file"}


@dataclass(frozen=True)
class SchedulerResult:
    """Result returned by :class:`WorkerScheduler` (REQ-M3-030)."""

    handoffs: list[WorkerHandoff] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)
    cycle_detected: bool = False


class _SchedulerSafetyStop(Exception):
    """Internal wrapper carrying whether the current assignment finished."""

    def __init__(self, original: SafetyStopError, *, current_completed: bool) -> None:
        super().__init__(str(original))
        self.original = original
        self.current_completed = current_completed


class WorkerScheduler:
    """Execute planner-provided assignments sequentially in topology order."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        worker_runtime: WorkerRuntime,
        cost_guard: CostGuard,
        workspace_manager: WorkspaceManager,
    ) -> None:
        self.repo = repo
        self.worker_runtime = worker_runtime
        self.cost_guard = cost_guard
        self.workspace_manager = workspace_manager

    async def execute_assignments(
        self,
        task_id: str,
        loop_id: int,
        assignments: list[Assignment],
        context_factory: Callable[[Assignment], ContextPack],
    ) -> SchedulerResult:
        """Run assignments in provided topology order with retry and skip handling."""
        assignment_ids = [assignment.assignment_id for assignment in assignments]
        if self._contains_cycle(assignments):
            for assignment in assignments:
                self._emit_assignment_skipped(
                    task_id,
                    loop_id,
                    assignment.assignment_id,
                    blocked_by="cycle",
                )
            return SchedulerResult(
                handoffs=[],
                skipped_ids=assignment_ids,
                cycle_detected=True,
            )

        self.workspace_manager.ensure_task_workspace(task_id)
        workspace_root = self.workspace_manager.candidate_files_dir(task_id, loop_id)
        workspace_root.mkdir(parents=True, exist_ok=True)

        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        skipped_ids: list[str] = []
        skipped_id_set: set[str] = set()
        final_handoffs: list[WorkerHandoff] = []
        evidence_by_assignment: dict[str, set[str]] = {}

        for index, assignment in enumerate(assignments):
            blocked_by = self._blocked_by(
                assignment,
                completed_ids=completed_ids,
                failed_ids=failed_ids,
                skipped_ids=skipped_id_set,
                all_assignment_ids=set(assignment_ids),
            )
            if blocked_by is not None:
                skipped_ids.append(assignment.assignment_id)
                skipped_id_set.add(assignment.assignment_id)
                self._emit_assignment_skipped(
                    task_id,
                    loop_id,
                    assignment.assignment_id,
                    blocked_by=blocked_by,
                )
                continue

            try:
                handoff = await self._run_assignment_with_retries(
                    task_id=task_id,
                    loop_id=loop_id,
                    assignment=assignment,
                    context_factory=context_factory,
                    workspace_root=workspace_root,
                    evidence_by_assignment=evidence_by_assignment,
                )
            except _SchedulerSafetyStop as exc:
                start = index + 1 if exc.current_completed else index
                for remaining in assignments[start:]:
                    if remaining.assignment_id in skipped_id_set:
                        continue
                    skipped_ids.append(remaining.assignment_id)
                    skipped_id_set.add(remaining.assignment_id)
                    self._emit_assignment_skipped(
                        task_id,
                        loop_id,
                        remaining.assignment_id,
                        blocked_by="safety_stop",
                    )
                raise exc.original

            final_handoffs.append(handoff)
            if self._handoff_failed(handoff):
                failed_ids.add(assignment.assignment_id)
            else:
                completed_ids.add(assignment.assignment_id)

        self._emit_write_collisions(
            task_id=task_id,
            loop_id=loop_id,
            evidence_by_assignment=evidence_by_assignment,
        )
        return SchedulerResult(
            handoffs=final_handoffs,
            skipped_ids=skipped_ids,
            cycle_detected=False,
        )

    async def _run_assignment_with_retries(
        self,
        *,
        task_id: str,
        loop_id: int,
        assignment: Assignment,
        context_factory: Callable[[Assignment], ContextPack],
        workspace_root: Path,
        evidence_by_assignment: dict[str, set[str]],
    ) -> WorkerHandoff:
        while True:
            attempt = assignment.retry_count + 1
            try:
                self.cost_guard.assert_within_budget(task_id)
            except SafetyStopError as exc:
                raise _SchedulerSafetyStop(
                    exc,
                    current_completed=False,
                ) from exc

            context = context_factory(assignment)
            if context.truncation_info is not None:
                self.repo.append_event(
                    EventType.CONTEXT_TRUNCATED,
                    context.truncation_info.model_dump(),
                    task_id=task_id,
                    loop_id=loop_id,
                )

            self.repo.append_event(
                EventType.WORKER_ASSIGNMENT_STARTED,
                {"assignment_id": assignment.assignment_id, "attempt": attempt},
                task_id=task_id,
                loop_id=loop_id,
            )
            spec = self.repo.get_agent_spec(assignment.agent_id)
            try:
                raw_result = await self.worker_runtime.run(
                    spec,
                    context,
                    workspace_root=workspace_root,
                )
            except SafetyStopError as exc:
                raise _SchedulerSafetyStop(
                    exc,
                    current_completed=False,
                ) from exc

            handoff = self._coerce_handoff(raw_result, assignment)
            evidence_by_assignment.setdefault(assignment.assignment_id, set()).update(
                handoff.evidence_ids
            )
            handoff_id, handoff = self._persist_handoff(
                task_id=task_id,
                loop_id=loop_id,
                assignment_id=assignment.assignment_id,
                handoff=handoff,
            )

            if self._handoff_failed(handoff):
                self.repo.append_event(
                    EventType.WORKER_ASSIGNMENT_FAILED,
                    {
                        "assignment_id": assignment.assignment_id,
                        "error_type": handoff.error_type or "unknown",
                        "handoff_id": handoff_id,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
            else:
                self.repo.append_event(
                    EventType.WORKER_ASSIGNMENT_COMPLETED,
                    {
                        "assignment_id": assignment.assignment_id,
                        "handoff_id": handoff_id,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )

            try:
                self.cost_guard.assert_within_budget(task_id)
            except SafetyStopError as exc:
                raise _SchedulerSafetyStop(
                    exc,
                    current_completed=True,
                ) from exc

            if (
                self._handoff_failed(handoff)
                and handoff.retryable
                and assignment.retry_count < assignment.max_retries
            ):
                assignment.retry_count += 1
                self.repo.append_event(
                    EventType.WORKER_ASSIGNMENT_RETRIED,
                    {
                        "assignment_id": assignment.assignment_id,
                        "attempt": assignment.retry_count + 1,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )
                continue

            return handoff

    def _persist_handoff(
        self,
        *,
        task_id: str,
        loop_id: int,
        assignment_id: str,
        handoff: WorkerHandoff,
    ) -> tuple[str, WorkerHandoff]:
        handoff_id = handoff.handoff_id or f"WH-{task_id}-{loop_id}-{assignment_id}"
        handoff = handoff.model_copy(update={"handoff_id": handoff_id})
        handoff_id = self.repo.save_worker_handoff(handoff)
        loop_root = (
            self.workspace_manager.task_root(task_id)
            / "candidates"
            / f"loop_{loop_id:03d}"
        )
        handoffs_dir = resolve_workspace_path(loop_root, "handoffs")
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        audit_path = resolve_workspace_path(handoffs_dir, f"{assignment_id}.json")
        if audit_path.parent != handoffs_dir.resolve():
            raise PermissionError(f"Invalid assignment audit path: {assignment_id}")
        audit_path.write_text(handoff.model_dump_json(indent=2), encoding="utf-8")
        return handoff_id, handoff

    def persist_handoff_audit(
        self,
        *,
        task_id: str,
        loop_id: int,
        assignment_id: str,
        handoff: WorkerHandoff,
    ) -> None:
        """Write a scheduler-compatible audit JSON without repository persistence."""
        loop_root = (
            self.workspace_manager.task_root(task_id)
            / "candidates"
            / f"loop_{loop_id:03d}"
        )
        handoffs_dir = resolve_workspace_path(loop_root, "handoffs")
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        audit_path = resolve_workspace_path(handoffs_dir, f"{assignment_id}.json")
        if audit_path.parent != handoffs_dir.resolve():
            raise PermissionError(f"Invalid assignment audit path: {assignment_id}")
        audit_path.write_text(handoff.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def _coerce_handoff(
        result: WorkerResult,
        assignment: Assignment,
    ) -> WorkerHandoff:
        if isinstance(result, WorkerHandoff):
            handoff = result
        else:
            handoff = WorkerHandoff(**result.model_dump())
        return handoff.model_copy(
            update={
                "assignment_id": assignment.assignment_id,
                "retry_count": assignment.retry_count,
            }
        )

    @staticmethod
    def _handoff_failed(handoff: WorkerHandoff) -> bool:
        return bool(handoff.error or handoff.error_type)

    @staticmethod
    def _contains_cycle(assignments: list[Assignment]) -> bool:
        assignment_ids = [assignment.assignment_id for assignment in assignments]
        if len(set(assignment_ids)) != len(assignment_ids):
            return True
        graph = {
            assignment.assignment_id: [
                dep for dep in assignment.depends_on if dep in assignment_ids
            ]
            for assignment in assignments
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(assignment_id: str) -> bool:
            if assignment_id in visiting:
                return True
            if assignment_id in visited:
                return False
            visiting.add(assignment_id)
            for dependency_id in graph[assignment_id]:
                if visit(dependency_id):
                    return True
            visiting.remove(assignment_id)
            visited.add(assignment_id)
            return False

        return any(visit(assignment_id) for assignment_id in assignment_ids)

    @staticmethod
    def _blocked_by(
        assignment: Assignment,
        *,
        completed_ids: set[str],
        failed_ids: set[str],
        skipped_ids: set[str],
        all_assignment_ids: set[str],
    ) -> str | None:
        for dependency_id in assignment.depends_on:
            if dependency_id in completed_ids:
                continue
            if dependency_id in failed_ids or dependency_id in skipped_ids:
                return dependency_id
            if dependency_id in all_assignment_ids:
                return dependency_id
            return dependency_id
        return None

    def _emit_assignment_skipped(
        self,
        task_id: str,
        loop_id: int,
        assignment_id: str,
        *,
        blocked_by: str,
    ) -> None:
        self.repo.append_event(
            EventType.WORKER_ASSIGNMENT_SKIPPED,
            {"assignment_id": assignment_id, "blocked_by": blocked_by},
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_write_collisions(
        self,
        *,
        task_id: str,
        loop_id: int,
        evidence_by_assignment: dict[str, set[str]],
    ) -> None:
        evidence_to_assignments: dict[str, set[str]] = {}
        for assignment_id, evidence_ids in evidence_by_assignment.items():
            for evidence_id in evidence_ids:
                evidence_to_assignments.setdefault(evidence_id, set()).add(
                    assignment_id
                )

        assignments_by_path: dict[str, set[str]] = {}
        for row in self.repo.list_successful_tool_call_evidence(task_id):
            if row.get("loop_id") != loop_id:
                continue
            row_evidence_id = row.get("evidence_id")
            if not isinstance(row_evidence_id, str):
                continue
            assignment_ids = evidence_to_assignments.get(row_evidence_id)
            if not assignment_ids:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            path = self._write_path_from_payload(payload)
            if path is None:
                continue
            assignments_by_path.setdefault(path, set()).update(assignment_ids)

        collisions = {
            path: sorted(assignment_ids)
            for path, assignment_ids in assignments_by_path.items()
            if len(assignment_ids) >= 2
        }
        if not collisions:
            return
        self.repo.append_event(
            EventType.WORKSPACE_WRITE_COLLISION,
            {
                "paths": sorted(collisions),
                "assignments_by_path": {
                    path: collisions[path] for path in sorted(collisions)
                },
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    @staticmethod
    def _write_path_from_payload(payload: dict[object, object]) -> str | None:
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name not in _WRITE_TOOLS:
            return None
        for key in ("path", "artifact_path", "write_path", "file_path"):
            path = payload.get(key)
            if isinstance(path, str) and path:
                return Path(path).as_posix()
        args = payload.get("args")
        if isinstance(args, dict):
            arg_path = args.get("path")
            if isinstance(arg_path, str) and arg_path:
                return Path(arg_path).as_posix()
        args_summary = payload.get("args_summary")
        if isinstance(args_summary, str):
            match = _PATH_RE.search(args_summary)
            if match is not None:
                return Path(match.group(1)).as_posix()
        return None
