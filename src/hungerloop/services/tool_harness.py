"""Tool entry point and policy layer (PRD §9 + §28.11).

ToolHarness is the *only* surface a Worker uses to take side effects. It
owns five concerns the per-tool implementations don't:

1. **Tool registry lookup** — unknown ``tool_name`` is a hard error
   (returned as :class:`ToolResult` with ``error_type='unknown_tool'``).
2. **Side-effect policy** — gates ``shell``, ``file_write`` and
   ``requires_network`` against :class:`BudgetAllocation` and raises
   :class:`ToolNotPermitted` so :class:`WorkerRuntime` can map it onto
   ``WorkerResult.error_type='tool_not_permitted'`` (PRD §28.11).
3. **BudgetGuard pre/post** — ``assert_can_spend(addl_tool_calls=1)``
   before dispatch, ``record(tool_calls=1, elapsed_seconds=...)`` after
   (PRD §28.4 contracts #4 and #5).
4. **Evidence** — every successful or failed dispatch writes a
   ``tool_call`` evidence row (PRD §28.5 / M18). RunShell adds a
   second row via :class:`SandboxRunner`; we keep both views.
5. **Artifacts** — ``write_file`` / ``patch_file`` outcomes carry an
   ``artifact_type``; :meth:`execute` persists the corresponding
   :class:`Artifact` and returns its id.

Tools never see :class:`ContextPack` — only ToolHarness does. This keeps
each tool a pure ``args -> outcome`` function and concentrates the
contract enforcement here.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from hungerloop.models.blackboard import Artifact
from hungerloop.models.context import ContextPack
from hungerloop.models.events import EventType
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_guard import BudgetGuard, WorkerBudgetExceeded
from hungerloop.services.tools import Tool


class ToolNotPermitted(RuntimeError):
    """Tool denied by per-loop policy (PRD §28.11 / M11).

    Raised before dispatch when ``budget.allow_*`` rejects a tool's
    side-effect class. :class:`WorkerRuntime` catches this at the same
    level as :class:`WorkerBudgetExceeded`.
    """


class ToolResult(BaseModel):
    """Harness-level result returned to :class:`ExecutionWorker` (PRD §8.2)."""

    tool_name: str
    success: bool
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None


class ToolHarness:
    """Single tool entry point with policy, budget, evidence, and artifacts."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        tool_registry: dict[str, Tool],
        budget_guard: BudgetGuard,
    ) -> None:
        self.repo = repo
        self.tool_registry = tool_registry
        self.budget_guard = budget_guard

    async def execute(
        self,
        context: ContextPack,
        tool_name: str,
        args: dict[str, object],
        workspace_root: Path,
    ) -> ToolResult:
        """Dispatch a tool call with full policy enforcement.

        Returns:
            :class:`ToolResult`. ``success=True`` only when the tool ran
            normally and produced its expected effect; ``error_type``
            distinguishes ``unknown_tool``, ``invalid_args``, and the
            tool-specific failure modes surfaced by
            :class:`ToolOutcome.success`.

        Raises:
            ToolNotPermitted: ``budget.allow_*`` denies this tool's
                side-effect class. The error is *raised* (not encoded in
                ToolResult) so :class:`WorkerRuntime` can re-raise via the
                same pathway as :class:`WorkerBudgetExceeded`.
        """
        # PRD §7.5: TOOL_CALL_STARTED fires once per execute() call,
        # before unknown-tool / policy / budget rejections so the audit
        # trail shows every attempted call (not just successful dispatches).
        self.repo.append_event(
            EventType.TOOL_CALL_STARTED,
            {
                "tool_name": tool_name,
                "agent_id": context.agent_id,
                "args_summary": self._summarize_args(args),
            },
            task_id=context.task_id,
            loop_id=context.loop_id,
        )

        tool = self.tool_registry.get(tool_name)
        if tool is None:
            evidence_id = self.repo.save_tool_call_as_evidence(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=context.agent_id,
                tool_name=tool_name,
                args_summary=self._summarize_args(args),
                result_summary=f"unknown tool: {tool_name}",
                success=False,
                elapsed_ms=0,
            )
            self.repo.append_event(
                EventType.TOOL_CALL_FAILED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "error_type": "unknown_tool",
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                summary=f"unknown tool: {tool_name}",
                evidence_ids=[evidence_id],
                error=f"unknown_tool:{tool_name}",
                error_type="unknown_tool",
            )

        try:
            self._enforce_side_effect_policy(tool, context)
        except ToolNotPermitted:
            # §7.5 invariant: every TOOL_CALL_STARTED has a terminal
            # twin. Emit FAILED with error_type="not_permitted" before
            # propagating so audit aggregations don't under-count
            # policy denials (post-review I5).
            self.repo.append_event(
                EventType.TOOL_CALL_FAILED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "error_type": "not_permitted",
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
            raise
        try:
            self.budget_guard.assert_can_spend(context, addl_tool_calls=1)
        except WorkerBudgetExceeded:
            self.repo.append_event(
                EventType.TOOL_CALL_FAILED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "error_type": "budget_exceeded",
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
            raise

        started = time.monotonic()
        try:
            outcome = await tool.run(
                args=args,
                workspace_root=workspace_root,
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
        except Exception as exc:
            elapsed_seconds = time.monotonic() - started
            elapsed_ms = int(elapsed_seconds * 1000)
            error_type = (
                "invalid_args"
                if isinstance(exc, (ValueError, PermissionError))
                else "tool_exception"
            )
            summary = f"{type(exc).__name__}: {exc}"
            evidence_id = self.repo.save_tool_call_as_evidence(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=context.agent_id,
                tool_name=tool_name,
                args_summary=self._summarize_args(args),
                result_summary=summary[:1000],
                success=False,
                elapsed_ms=elapsed_ms,
            )
            self.budget_guard.record(
                context.task_id,
                context.loop_id,
                context.agent_id,
                tool_calls=1,
                elapsed_seconds=elapsed_seconds,
            )
            self.repo.append_event(
                EventType.TOOL_CALL_FAILED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "error_type": error_type,
                    "elapsed_ms": elapsed_ms,
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                summary=summary,
                evidence_ids=[evidence_id],
                artifact_ids=[],
                error=summary,
                error_type=error_type,
            )
        elapsed_seconds = time.monotonic() - started
        elapsed_ms = int(elapsed_seconds * 1000)

        evidence_id = self.repo.save_tool_call_as_evidence(
            task_id=context.task_id,
            loop_id=context.loop_id,
            agent_id=context.agent_id,
            tool_name=tool_name,
            args_summary=outcome.args_summary,
            result_summary=outcome.result_summary,
            success=outcome.success,
            elapsed_ms=elapsed_ms,
        )
        evidence_ids: list[str] = [evidence_id]
        if (
            outcome.sandbox_result is not None
            and outcome.sandbox_result.evidence_id is not None
        ):
            evidence_ids.append(outcome.sandbox_result.evidence_id)

        artifact_ids: list[str] = []
        if outcome.success and outcome.artifact_type is not None:
            artifact = Artifact(
                artifact_id=f"art-{uuid.uuid4().hex[:8]}",
                task_id=context.task_id,
                loop_id=context.loop_id,
                artifact_type=outcome.artifact_type,
                path=outcome.artifact_path,
                summary=outcome.artifact_summary,
            )
            self.repo.save_artifact(artifact)
            artifact_ids.append(artifact.artifact_id)

        self.budget_guard.record(
            context.task_id,
            context.loop_id,
            context.agent_id,
            tool_calls=1,
            elapsed_seconds=elapsed_seconds,
        )

        if outcome.success:
            self.repo.append_event(
                EventType.TOOL_CALL_SUCCEEDED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "elapsed_ms": elapsed_ms,
                    "artifact_count": len(artifact_ids),
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )
        else:
            self.repo.append_event(
                EventType.TOOL_CALL_FAILED,
                {
                    "tool_name": tool_name,
                    "agent_id": context.agent_id,
                    "error_type": "tool_failed",
                    "elapsed_ms": elapsed_ms,
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )

        return ToolResult(
            tool_name=tool_name,
            success=outcome.success,
            summary=outcome.summary,
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
            error=None if outcome.success else outcome.summary,
            error_type=None if outcome.success else "tool_failed",
        )

    @staticmethod
    def _summarize_args(args: dict[str, object], limit: int = 1000) -> str:
        text = repr(args)
        return text if len(text) <= limit else text[:limit]

    @staticmethod
    def _enforce_side_effect_policy(tool: Tool, context: ContextPack) -> None:
        """Apply PRD §28.11 / M11 gates."""
        budget = context.budget
        if tool.side_effect_level == "shell" and not budget.allow_shell:
            raise ToolNotPermitted(f"shell disabled by budget: {tool.name}")
        if tool.side_effect_level == "file_write" and not budget.allow_file_write:
            raise ToolNotPermitted(f"file_write disabled by budget: {tool.name}")
        if tool.requires_network and not budget.allow_network:
            raise ToolNotPermitted(f"network disabled by budget: {tool.name}")
