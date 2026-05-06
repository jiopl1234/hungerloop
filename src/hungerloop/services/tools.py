"""Built-in tool implementations for HungerLoop v0.5a (PRD §9.2).

Each tool is a small async-callable class. The :class:`Tool` Protocol is
the surface :class:`ToolHarness` consumes — every tool advertises its
``side_effect_level`` and ``requires_network`` so the harness can gate
execution against :class:`BudgetAllocation` (PRD §28.11 / M11).

The four MVP tools cover the actions a worker can plausibly emit on a
candidate workspace:

* ``read_file`` — surfaces a file's content into evidence.
* ``write_file`` — overwrites/creates a file under the candidate workspace.
* ``patch_file`` — single-occurrence find/replace; deterministic and
  rejects ambiguous matches so a non-LLM caller cannot silently corrupt
  files.
* ``run_shell`` — argv-only shell (PRD §9.5); delegates to
  :class:`SandboxRunner` for subprocess isolation.

Tools never write evidence or artifacts themselves. They return a typed
:class:`ToolOutcome` and let :class:`ToolHarness` own persistence so we
have one place that enforces evidence-on-every-call (PRD §28.5 / M18).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner, SandboxRunResult

SideEffectLevel = Literal["read", "file_write", "shell"]
"""Tool side-effect tiers gated by :class:`BudgetAllocation` (PRD §28.11):

* ``"read"`` — never gated.
* ``"file_write"`` — gated by ``budget.allow_file_write``.
* ``"shell"`` — gated by ``budget.allow_shell``.
"""


class ToolOutcome(BaseModel):
    """Per-tool return shape consumed by :class:`ToolHarness`.

    Tools never write evidence themselves; they hand back the data the
    harness needs to write a single ``tool_call`` evidence row plus any
    artifact metadata (path/summary).
    """

    success: bool
    summary: str = ""
    args_summary: str = ""
    result_summary: str = ""

    artifact_type: str | None = None
    artifact_path: str | None = None
    artifact_summary: str = ""

    sandbox_result: SandboxRunResult | None = None


class Tool(Protocol):
    """Minimal contract every tool must satisfy (PRD §9.3).

    ``task_id`` / ``loop_id`` flow through the call site so that tools
    delegating to :class:`SandboxRunner` (which already writes its own
    evidence row) can tag the subprocess output with the same loop the
    harness will use for the ``tool_call`` envelope row.
    """

    name: str
    args_schema: str
    side_effect_level: SideEffectLevel
    requires_network: bool

    async def run(
        self,
        *,
        args: dict[str, object],
        workspace_root: Path,
        task_id: str,
        loop_id: int,
    ) -> ToolOutcome: ...


def _stringify(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _truncate(text: str, limit: int = 2000) -> str:
    """Cap free-text fields stored in evidence (PRD §28.5)."""
    return text if len(text) <= limit else text[:limit]


class ReadFileTool:
    """Read a UTF-8 file from the candidate workspace."""

    name: str = "read_file"
    args_schema: str = "args = {path: str (required)}"
    side_effect_level: SideEffectLevel = "read"
    requires_network: bool = False

    async def run(
        self,
        *,
        args: dict[str, object],
        workspace_root: Path,
        task_id: str,
        loop_id: int,
    ) -> ToolOutcome:
        del task_id, loop_id  # unused; tool reads from workspace only
        path_arg = _stringify(args.get("path", ""))
        safe = resolve_workspace_path(workspace_root, path_arg)

        if not safe.is_file():
            return ToolOutcome(
                success=False,
                summary=f"file not found: {path_arg}",
                args_summary=f"path={path_arg}",
                result_summary="not_found",
            )

        content = safe.read_text(encoding="utf-8", errors="replace")
        preview = _truncate(content)
        return ToolOutcome(
            success=True,
            summary=f"read {len(content)} chars from {path_arg}",
            args_summary=f"path={path_arg}",
            result_summary=preview,
        )


class WriteFileTool:
    """Create or overwrite a UTF-8 file under the candidate workspace."""

    name: str = "write_file"
    args_schema: str = (
        "args = {path: str (required), content: str (required)}"
    )
    side_effect_level: SideEffectLevel = "file_write"
    requires_network: bool = False

    async def run(
        self,
        *,
        args: dict[str, object],
        workspace_root: Path,
        task_id: str,
        loop_id: int,
    ) -> ToolOutcome:
        del task_id, loop_id
        path_arg = _stringify(args.get("path", ""))
        content = _stringify(args.get("content", ""))
        safe = resolve_workspace_path(workspace_root, path_arg)

        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")

        return ToolOutcome(
            success=True,
            summary=f"wrote {len(content)} chars to {path_arg}",
            args_summary=f"path={path_arg} bytes={len(content)}",
            result_summary=f"wrote {len(content)} chars",
            artifact_type="file_write",
            artifact_path=path_arg,
            artifact_summary=f"write_file {path_arg}",
        )


class PatchFileTool:
    """Replace a single, unique occurrence of ``old_text`` in a file.

    The non-uniqueness rule prevents accidental corruption when the
    caller is non-LLM (e.g. a deterministic test script) — it also lets
    us avoid shipping a real diff/patch parser in v0.5a.
    """

    name: str = "patch_file"
    args_schema: str = (
        "args = {path: str (required), old_text: str (required), "
        "new_text: str (required)}"
    )
    side_effect_level: SideEffectLevel = "file_write"
    requires_network: bool = False

    async def run(
        self,
        *,
        args: dict[str, object],
        workspace_root: Path,
        task_id: str,
        loop_id: int,
    ) -> ToolOutcome:
        del task_id, loop_id
        path_arg = _stringify(args.get("path", ""))
        old_text = _stringify(args.get("old_text", ""))
        new_text = _stringify(args.get("new_text", ""))
        safe = resolve_workspace_path(workspace_root, path_arg)

        if not safe.is_file():
            return ToolOutcome(
                success=False,
                summary=f"file not found: {path_arg}",
                args_summary=f"path={path_arg}",
                result_summary="not_found",
            )

        original = safe.read_text(encoding="utf-8")
        occurrences = original.count(old_text)
        if occurrences == 0:
            return ToolOutcome(
                success=False,
                summary=f"old_text not found in {path_arg}",
                args_summary=f"path={path_arg}",
                result_summary="no_match",
            )
        if occurrences > 1:
            return ToolOutcome(
                success=False,
                summary=f"old_text matches {occurrences} times in {path_arg}",
                args_summary=f"path={path_arg}",
                result_summary=f"ambiguous:{occurrences}",
            )

        patched = original.replace(old_text, new_text, 1)
        safe.write_text(patched, encoding="utf-8")

        return ToolOutcome(
            success=True,
            summary=f"patched {path_arg}",
            args_summary=(
                f"path={path_arg} "
                f"old_len={len(old_text)} new_len={len(new_text)}"
            ),
            result_summary=f"replaced {len(old_text)} -> {len(new_text)} chars",
            artifact_type="file_patch",
            artifact_path=path_arg,
            artifact_summary=f"patch_file {path_arg}",
        )


class RunShellTool:
    """argv-only shell (PRD §9.5) routed through :class:`SandboxRunner`.

    The sandbox runner handles subprocess isolation (process group, output
    cap, timeout cleanup) and writes its own ``shell_output`` evidence row.
    The ToolHarness writes a separate ``tool_call`` envelope on top so
    operators see both the per-tool view and the per-shell view (PRD §28.5).
    """

    name: str = "run_shell"
    args_schema: str = (
        "args = {argv: list[str] (required, non-empty), timeout: int = 60}"
    )
    side_effect_level: SideEffectLevel = "shell"
    requires_network: bool = False

    def __init__(self, sandbox_runner: SandboxRunner) -> None:
        self._sandbox = sandbox_runner

    async def run(
        self,
        *,
        args: dict[str, object],
        workspace_root: Path,
        task_id: str,
        loop_id: int,
    ) -> ToolOutcome:
        argv_raw = args.get("argv")
        if not isinstance(argv_raw, list) or not argv_raw:
            return ToolOutcome(
                success=False,
                summary="run_shell requires non-empty argv list",
                args_summary="argv=<missing>",
                result_summary="bad_args",
            )
        argv = [_stringify(item) for item in argv_raw]

        timeout_raw = args.get("timeout", 60)
        timeout = (
            int(timeout_raw) if isinstance(timeout_raw, (int, float, str)) else 60
        )

        sandbox_result = await self._sandbox.run_argv(
            task_id=task_id,
            loop_id=loop_id,
            argv=argv,
            cwd=workspace_root,
            timeout=timeout,
            evidence_label="run_shell",
        )

        success = sandbox_result.exit_code == 0 and not sandbox_result.timed_out
        result_summary = (
            f"exit={sandbox_result.exit_code} "
            f"timed_out={sandbox_result.timed_out}"
        )
        return ToolOutcome(
            success=success,
            summary=(
                f"run_shell {' '.join(argv)} -> exit={sandbox_result.exit_code}"
            ),
            args_summary=f"argv={argv} timeout={timeout}",
            result_summary=result_summary,
            sandbox_result=sandbox_result,
        )


BUILTIN_TOOL_ARG_SCHEMAS: dict[str, str] = {
    ReadFileTool.name: ReadFileTool.args_schema,
    WriteFileTool.name: WriteFileTool.args_schema,
    PatchFileTool.name: PatchFileTool.args_schema,
    RunShellTool.name: RunShellTool.args_schema,
}


def describe_tool_arg_schemas(allowed_tools: list[str]) -> str:
    """Return prompt-ready args schemas for the tools a worker may call."""
    tool_names = allowed_tools or list(BUILTIN_TOOL_ARG_SCHEMAS)
    lines: list[str] = []
    for tool_name in tool_names:
        schema = BUILTIN_TOOL_ARG_SCHEMAS.get(tool_name)
        if schema is None:
            lines.append(f"- {tool_name}: args schema unavailable")
        else:
            lines.append(f"- {tool_name}: {schema}")
    return "\n".join(lines) if lines else "- (none)"


def default_tool_registry(sandbox_runner: SandboxRunner) -> dict[str, Tool]:
    """Build the four-tool registry used by the v0.5a Worker (PRD §9.2)."""
    return {
        ReadFileTool.name: ReadFileTool(),
        WriteFileTool.name: WriteFileTool(),
        PatchFileTool.name: PatchFileTool(),
        RunShellTool.name: RunShellTool(sandbox_runner),
    }
