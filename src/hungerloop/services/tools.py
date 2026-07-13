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

from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner, SandboxRunResult

# Total diagnostic envelope size for failed patch_file calls. The whole
# rendered ToolOutcome.summary (header + closest matches / occurrences)
# is capped at this many characters so the inner-loop follow-up message
# never balloons even on huge files.
_PATCH_DIAGNOSTIC_CHAR_BUDGET = 1500
_PATCH_DIAGNOSTIC_MAX_CLOSEST = 3
_PATCH_DIAGNOSTIC_MAX_OCCURRENCES = 5
# Single-line clip so one absurdly long source line cannot eat the whole
# diagnostic budget by itself.
_PATCH_DIAGNOSTIC_LINE_CLIP = 200


def _clip_line(line: str, limit: int = _PATCH_DIAGNOSTIC_LINE_CLIP) -> str:
    """Trim a single source line for inclusion in the diagnostic block."""
    if len(line) <= limit:
        return line
    head = limit // 2
    tail = limit - head - 1
    return f"{line[:head]}…{line[-tail:]}"


def _first_line(text: str) -> str:
    """Take the first line of a multi-line target for the diagnostic header."""
    if not text:
        return ""
    return text.split("\n", 1)[0]


def _closest_match_diagnostic(
    *,
    original: str,
    old_text: str,
    max_matches: int = _PATCH_DIAGNOSTIC_MAX_CLOSEST,
    line_offset: int = 0,
) -> str:
    """Render the top-N lines most similar to ``old_text`` for no_match.

    Uses difflib.SequenceMatcher on the FIRST line of ``old_text`` against
    each line in the file so we don't accidentally rank long multi-line
    targets by total length. Tool-agnostic: works for any text file.
    """
    needle = _first_line(old_text).strip()
    if not needle:
        return ""
    lines = original.splitlines()
    if not lines:
        return ""
    scored: list[tuple[float, int, str]] = []
    for idx, raw in enumerate(lines, start=1 + line_offset):
        ratio = SequenceMatcher(None, needle, raw.strip()).ratio()
        scored.append((ratio, idx, raw))
    scored.sort(key=lambda tup: (-tup[0], tup[1]))
    out: list[str] = ["closest_matches:"]
    for ratio, lineno, raw in scored[:max_matches]:
        if ratio <= 0.0:
            break
        out.append(
            f"  L{lineno} (ratio={ratio:.2f}): {_clip_line(raw)}"
        )
    return "\n".join(out) if len(out) > 1 else ""


def _ambiguous_occurrences_diagnostic(
    *,
    original: str,
    old_text: str,
    max_occurrences: int = _PATCH_DIAGNOSTIC_MAX_OCCURRENCES,
    line_offset: int = 0,
) -> str:
    """Render line numbers + 1 line of context for each occurrence."""
    if not old_text:
        return ""
    lines = original.splitlines()
    if not lines:
        return ""
    needle_first = _first_line(old_text)
    hits: list[int] = []
    for idx, raw in enumerate(lines, start=1 + line_offset):
        if needle_first and needle_first in raw:
            hits.append(idx)
            if len(hits) >= max_occurrences:
                break
    if not hits:
        # Fallback: scan via raw count if needle_first didn't catch lines
        # (e.g. needle spans multiple lines and no single-line surrogate
        # exists). Skip diagnostic in that edge case rather than mislead.
        return ""
    out: list[str] = [f"occurrences (showing up to {max_occurrences}):"]
    for lineno in hits:
        local_index = lineno - line_offset - 1
        target = lines[local_index]
        prev_ctx = lines[local_index - 1] if local_index - 1 >= 0 else ""
        next_ctx = lines[local_index + 1] if local_index + 1 < len(lines) else ""
        out.append(f"  L{lineno}: {_clip_line(target)}")
        if prev_ctx or next_ctx:
            ctx_pieces: list[str] = []
            if prev_ctx:
                ctx_pieces.append(f"prev=L{lineno - 1}:{_clip_line(prev_ctx)}")
            if next_ctx:
                ctx_pieces.append(f"next=L{lineno + 1}:{_clip_line(next_ctx)}")
            out.append("    " + " | ".join(ctx_pieces))
    return "\n".join(out)


def _positive_line_number(value: object, *, name: str) -> int | None:
    """Parse an optional one-based line anchor without accepting booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value) if isinstance(value, (int, str)) else 0
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _canonical_patch_line(line: str) -> str:
    """Collapse all whitespace on one line for resilient patch matching."""
    return " ".join(line.split())


def _normalized_line_matches(
    *,
    original_lines: list[str],
    old_text: str,
) -> list[tuple[int, int]]:
    """Return line-window matches after per-line whitespace normalization."""
    old_lines = old_text.splitlines()
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    if not old_lines:
        return []

    needle = [_canonical_patch_line(line) for line in old_lines]
    haystack = [
        _canonical_patch_line(line.rstrip("\r\n")) for line in original_lines
    ]
    width = len(needle)
    return [
        (index, index + width)
        for index in range(0, len(haystack) - width + 1)
        if haystack[index : index + width] == needle
    ]


def _replacement_preserving_line_ending(segment: str, new_text: str) -> str:
    """Keep the source line terminator for a normalized full-line match."""
    if new_text.endswith(("\n", "\r")):
        return new_text
    if segment.endswith("\r\n"):
        return new_text + "\r\n"
    if segment.endswith("\n"):
        return new_text + "\n"
    if segment.endswith("\r"):
        return new_text + "\r"
    return new_text


def _leading_indent_of_first_content_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def _reindent_normalized_replacement(
    segment: str,
    old_text: str,
    new_text: str,
) -> str | None:
    """Preserve source indentation when old/new line structure is unchanged."""
    source_lines = segment.splitlines()
    old_lines = old_text.splitlines()
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    new_lines = new_text.splitlines(keepends=True)
    if len(source_lines) != len(old_lines) or len(new_lines) != len(old_lines):
        return None

    rebased: list[str] = []
    for source_line, expected_line, replacement_line in zip(
        source_lines,
        old_lines,
        new_lines,
        strict=True,
    ):
        expected_has_content = bool(expected_line.strip())
        replacement_has_content = bool(replacement_line.strip())
        if expected_has_content != replacement_has_content:
            return None
        if not replacement_has_content:
            rebased.append(replacement_line)
            continue
        expected_indent = _leading_indent_of_first_content_line(expected_line)
        replacement_indent = _leading_indent_of_first_content_line(replacement_line)
        if expected_indent != replacement_indent:
            return None
        source_indent = _leading_indent_of_first_content_line(source_line)
        rebased.append(source_indent + replacement_line[len(replacement_indent) :])
    return "".join(rebased)


def _cap_diagnostic(text: str, limit: int = _PATCH_DIAGNOSTIC_CHAR_BUDGET) -> str:
    """Hard cap the entire patch_file diagnostic blob."""
    if len(text) <= limit:
        return text
    marker = "\n…[diagnostic truncated]"
    return text[: max(0, limit - len(marker))] + marker

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


READ_FILE_MAX_LINES = 400
READ_FILE_RESULT_CHARS = 8000


class ReadFileTool:
    """Read a UTF-8 file from the candidate workspace."""

    name: str = "read_file"
    args_schema: str = (
        "args = {path: str (required), offset: int (optional, 1-based line, "
        "default 1), limit: int (optional, 1-400 lines, default 200)}"
    )
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
        lines = content.splitlines(keepends=True)
        offset_raw = args.get("offset")
        limit_raw = args.get("limit")
        offset = 1 if offset_raw is None else offset_raw
        limit = 200 if limit_raw is None else limit_raw
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 1
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            return ToolOutcome(
                success=False,
                summary="read_file offset/limit must be positive integers",
                args_summary=f"path={path_arg}",
                result_summary="bad_args",
            )
        limit = min(limit, READ_FILE_MAX_LINES)
        total_lines = len(lines)
        if offset > max(1, total_lines):
            return ToolOutcome(
                success=False,
                summary=(
                    f"read_file offset {offset} is beyond {total_lines} lines "
                    f"in {path_arg}"
                ),
                args_summary=f"path={path_arg} offset={offset} limit={limit}",
                result_summary="offset_out_of_range",
            )

        selected_lines = lines[offset - 1 : offset - 1 + limit]
        selected = "".join(selected_lines)
        if total_lines == 0:
            range_label = "lines 0-0 of 0"
        else:
            end_line = offset + len(selected_lines) - 1
            range_label = f"lines {offset}-{end_line} of {total_lines}"
        preview = _truncate(
            f"[{range_label}]\n{selected}",
            READ_FILE_RESULT_CHARS,
        )
        return ToolOutcome(
            success=True,
            summary=f"read {range_label} from {path_arg}",
            args_summary=f"path={path_arg} offset={offset} limit={limit}",
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
    """Replace one unique exact or whitespace-normalized match in a file.

    The non-uniqueness rule prevents accidental corruption when the
    caller is non-LLM (e.g. a deterministic test script) — it also lets
    us avoid shipping a real diff/patch parser in v0.5a.
    """

    name: str = "patch_file"
    args_schema: str = (
        "args = {path: str (required), old_text: str (required), "
        "new_text: str (required), start_line: int >= 1 (optional), "
        "end_line: int >= start_line (optional)}"
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

        if not old_text:
            return ToolOutcome(
                success=False,
                summary="patch_file requires non-empty old_text",
                args_summary=f"path={path_arg}",
                result_summary="invalid_old_text",
            )

        try:
            start_line = _positive_line_number(
                args.get("start_line"), name="start_line"
            )
            end_line = _positive_line_number(args.get("end_line"), name="end_line")
        except ValueError as exc:
            return ToolOutcome(
                success=False,
                summary=str(exc),
                args_summary=f"path={path_arg}",
                result_summary="invalid_line_anchor",
            )

        if not safe.is_file():
            return ToolOutcome(
                success=False,
                summary=f"file not found: {path_arg}",
                args_summary=f"path={path_arg}",
                result_summary="not_found",
            )

        original = safe.read_text(encoding="utf-8")
        original_lines = original.splitlines(keepends=True)
        total_lines = len(original_lines)
        if total_lines == 0:
            return ToolOutcome(
                success=False,
                summary=(
                    f"old_text not found in {path_arg}\n"
                    f"old_text_preview ({len(old_text)} chars): "
                    f"{_clip_line(_first_line(old_text))}"
                ),
                args_summary=f"path={path_arg}",
                result_summary="no_match",
            )
        effective_start = start_line or 1
        effective_end = end_line or total_lines
        if (
            effective_start > effective_end
            or effective_start > total_lines
            or effective_end > total_lines
        ):
            return ToolOutcome(
                success=False,
                summary=(
                    f"invalid line anchor for {path_arg}: requested "
                    f"{effective_start}-{effective_end}, file has {total_lines} lines"
                ),
                args_summary=f"path={path_arg}",
                result_summary="invalid_line_anchor",
            )

        region_start = sum(len(line) for line in original_lines[: effective_start - 1])
        region_end = sum(len(line) for line in original_lines[:effective_end])
        region = original[region_start:region_end]
        occurrences = region.count(old_text)
        if occurrences == 0:
            region_lines = original_lines[effective_start - 1 : effective_end]
            normalized_matches = _normalized_line_matches(
                original_lines=region_lines,
                old_text=old_text,
            )
            if len(normalized_matches) == 1:
                normalized_fallback_allowed = (
                    start_line is not None
                    or end_line is not None
                    or len(old_text.splitlines()) >= 2
                )
                if not normalized_fallback_allowed:
                    return ToolOutcome(
                        success=False,
                        summary=(
                            "whitespace-normalized single-line patch requires a "
                            f"line anchor in {path_arg}; provide start_line/end_line "
                            "or use a multi-line old_text"
                        ),
                        args_summary=f"path={path_arg}",
                        result_summary="unsafe_normalized_single_line",
                    )
                local_start_line, local_end_line = normalized_matches[0]
                local_start = sum(len(line) for line in region_lines[:local_start_line])
                local_end = sum(len(line) for line in region_lines[:local_end_line])
                match_start = region_start + local_start
                match_end = region_start + local_end
                segment = original[match_start:match_end]
                rebased_replacement = _reindent_normalized_replacement(
                    segment,
                    old_text,
                    new_text,
                )
                if rebased_replacement is None:
                    return ToolOutcome(
                        success=False,
                        summary=(
                            "whitespace-normalized patch has an unsafe indentation "
                            f"rebase in {path_arg}; re-read the enclosing block and "
                            "use an exact patch or write_file"
                        ),
                        args_summary=f"path={path_arg}",
                        result_summary="unsafe_normalized_indentation",
                    )
                replacement = _replacement_preserving_line_ending(
                    segment,
                    rebased_replacement,
                )
                patched = original[:match_start] + replacement + original[match_end:]
                safe.write_text(patched, encoding="utf-8")
                matched_start_line = effective_start + local_start_line
                matched_end_line = effective_start + local_end_line - 1
                return ToolOutcome(
                    success=True,
                    summary=(
                        f"patched {path_arg} using whitespace-normalized lines "
                        f"{matched_start_line}-{matched_end_line}"
                    ),
                    args_summary=(
                        f"path={path_arg} old_len={len(old_text)} "
                        f"new_len={len(new_text)} match=whitespace_normalized"
                    ),
                    result_summary=(
                        f"replaced normalized lines "
                        f"{matched_start_line}-{matched_end_line}"
                    ),
                    artifact_type="file_patch",
                    artifact_path=path_arg,
                    artifact_summary=f"patch_file {path_arg}",
                )
            if len(normalized_matches) > 1:
                matched_lines = [
                    str(effective_start + match[0])
                    for match in normalized_matches[:_PATCH_DIAGNOSTIC_MAX_OCCURRENCES]
                ]
                return ToolOutcome(
                    success=False,
                    summary=_cap_diagnostic(
                        f"old_text whitespace-normalized match is ambiguous in "
                        f"{path_arg}: {len(normalized_matches)} matches at lines "
                        + ", ".join(matched_lines)
                    ),
                    args_summary=f"path={path_arg}",
                    result_summary=f"ambiguous_normalized:{len(normalized_matches)}",
                )
            header = (
                f"old_text not found in {path_arg}\n"
                f"old_text_preview ({len(old_text)} chars): "
                f"{_clip_line(_first_line(old_text))}"
            )
            closest = _closest_match_diagnostic(
                original=region,
                old_text=old_text,
                line_offset=effective_start - 1,
            )
            body = f"{header}\n{closest}" if closest else header
            return ToolOutcome(
                success=False,
                summary=_cap_diagnostic(body),
                args_summary=f"path={path_arg}",
                result_summary="no_match",
            )
        if occurrences > 1:
            header = (
                f"old_text matches {occurrences} times in {path_arg}\n"
                f"old_text_preview ({len(old_text)} chars): "
                f"{_clip_line(_first_line(old_text))}"
            )
            occ_block = _ambiguous_occurrences_diagnostic(
                original=region,
                old_text=old_text,
                line_offset=effective_start - 1,
            )
            body = f"{header}\n{occ_block}" if occ_block else header
            return ToolOutcome(
                success=False,
                summary=_cap_diagnostic(body),
                args_summary=f"path={path_arg}",
                result_summary=f"ambiguous:{occurrences}",
            )

        local_match_start = region.find(old_text)
        match_start = region_start + local_match_start
        match_end = match_start + len(old_text)
        patched = original[:match_start] + new_text + original[match_end:]
        safe.write_text(patched, encoding="utf-8")

        return ToolOutcome(
            success=True,
            summary=f"patched {path_arg}",
            args_summary=(
                f"path={path_arg} "
                f"old_len={len(old_text)} new_len={len(new_text)} match=exact"
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
