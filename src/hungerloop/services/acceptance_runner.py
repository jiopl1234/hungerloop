"""Acceptance check runner for HungerLoop v0.4.1.

:class:`AcceptanceCheckRunner` dispatches on :class:`AcceptanceCheckType` and
executes a single acceptance check against a candidate workspace. Implements
invariants I-5 (targeted validation: only the requested check runs) and I-7
(sandboxed checks: shell commands route through :class:`SandboxRunner` so they
inherit timeout enforcement and evidence capture).

Each ``run`` call returns ``(passed, detail, evidence_id)``; the
:class:`~hungerloop.services.validation_gate.ValidationGate` wraps the tuple
into a :class:`~hungerloop.models.validation.CheckResult`.
"""
from __future__ import annotations

from typing import Any

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner, SandboxRunResult
from hungerloop.services.workspace_manager import WorkspaceManager

SHELL_OUTPUT_SECTION_CHARS = 700


class AcceptanceCheckRunner:
    """Dispatch a single acceptance check against a candidate workspace."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        workspace_manager: WorkspaceManager,
        sandbox_runner: SandboxRunner,
    ) -> None:
        self.repo = repo
        self.workspace_manager = workspace_manager
        self.sandbox_runner = sandbox_runner

    async def run(
        self,
        check: Any,
        task_id: str,
        loop_id: int,
        candidate: Any,
    ) -> tuple[bool, str, str | None]:
        """Run a single acceptance check.

        Returns:
            A tuple ``(passed, detail, evidence_id)``. ``evidence_id`` is the
            shell-output evidence record id for SHELL_EXIT_ZERO checks; other
            check types return ``None`` because they emit no shell evidence.

        Raises:
            NotImplementedError: For ``LLM_JUDGE`` (deferred to V1.2+).
            ValueError: For unknown or malformed checks.
        """
        ct = check.check_type
        candidate_root = self.workspace_manager.candidate_files_dir(task_id, loop_id)

        if ct == AcceptanceCheckType.FILE_EXISTS:
            path = resolve_workspace_path(candidate_root, str(check.params["path"]))
            ok = path.exists() and path.is_file()
            return ok, f"file_exists({check.params['path']}): {ok}", None

        if ct == AcceptanceCheckType.SHELL_EXIT_ZERO:
            if "argv" not in check.params:
                raise ValueError("SHELL_EXIT_ZERO requires params.argv in MVP.")

            argv = list(check.params["argv"])
            timeout = int(check.params.get("timeout", 60))

            result = await self.sandbox_runner.run_argv(
                task_id=task_id,
                loop_id=loop_id,
                argv=argv,
                cwd=candidate_root,
                timeout=timeout,
                evidence_label=f"acceptance:{candidate.id}",
            )

            ok = result.exit_code == 0 and not result.timed_out
            detail = (
                f"shell_exit_zero(argv={argv}): "
                f"exit={result.exit_code}, timeout={result.timed_out}"
            )
            if not ok:
                output_summary = _summarize_shell_output(result)
                if output_summary:
                    detail = f"{detail}; {output_summary}"
            return ok, detail, result.evidence_id

        if ct == AcceptanceCheckType.EVIDENCE_COUNT_MIN:
            ev_type = str(check.params.get("evidence_type", "any"))
            min_count = int(check.params["min_count"])
            count: int = self.repo.count_evidence_by_type(
                task_id=task_id,
                evidence_ids=candidate.evidence_ids,
                evidence_type=ev_type,
                successful_only=True,
            )
            ok = count >= min_count
            return ok, f"evidence_count({ev_type}): {count}/{min_count}", None

        if ct == AcceptanceCheckType.ARTIFACT_TYPE_EXISTS:
            art_type = str(check.params["artifact_type"])
            artifacts = self.repo.get_artifacts_by_ids(candidate.artifact_ids)
            ok = any(a.artifact_type == art_type for a in artifacts)
            return ok, f"artifact_type_exists({art_type}): {ok}", None

        if ct == AcceptanceCheckType.HUMAN_APPROVAL:
            approval_id = str(check.params["approval_id"])
            approved: bool = self.repo.is_approval_granted(approval_id)
            return approved, f"human_approval({approval_id}): {approved}", None

        if ct == AcceptanceCheckType.LLM_JUDGE:
            raise NotImplementedError(
                "LLM_JUDGE is V1.2+. Use binary checks in MVP."
            )

        raise ValueError(f"Unknown check type: {ct}")


def _summarize_shell_output(result: SandboxRunResult) -> str:
    """Return a compact stdout/stderr excerpt for failed shell checks."""
    parts: list[str] = []
    if result.stdout.strip():
        parts.append(
            "stdout="
            + _clip_middle(
                _one_line(result.stdout),
                SHELL_OUTPUT_SECTION_CHARS,
            )
        )
    if result.stderr.strip():
        parts.append(
            "stderr="
            + _clip_middle(
                _one_line(result.stderr),
                SHELL_OUTPUT_SECTION_CHARS,
            )
        )
    return "; ".join(parts)


def _one_line(text: str) -> str:
    """Keep shell output prompt-safe without hiding traceback structure."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    return r"\n".join(lines)


def _clip_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    head_chars = max(1, (max_chars - 1) // 2)
    tail_chars = max(1, max_chars - 1 - head_chars)
    return f"{text[:head_chars]}…{text[-tail_chars:]}"
