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
from hungerloop.services.evidence_render import clip_head_tail
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner, SandboxRunResult
from hungerloop.services.workspace_manager import WorkspaceManager

SHELL_OUTPUT_SECTION_CHARS = 2000


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
            raw_pattern = str(check.params["path"])
            if any(ch in raw_pattern for ch in ("*", "?", "[")):
                # Glob pattern — Path.glob is scoped to candidate_root so
                # path safety is preserved. ANY match (>=1 regular file)
                # satisfies the existence check. Without this branch,
                # `*.py` was treated as a literal filename and the check
                # could never pass for glob-shaped acceptance criteria —
                # the worker would commit the real file, judge would
                # confirm it, but H-1:0 stayed pending forever and the
                # mission burned 5+ loops chasing a phantom check.
                matches = [p for p in candidate_root.glob(raw_pattern) if p.is_file()]
                ok = bool(matches)
                detail = (
                    f"file_exists({raw_pattern}): {ok} "
                    f"(glob matched {len(matches)} file(s))"
                )
                return ok, detail, None
            path = resolve_workspace_path(candidate_root, raw_pattern)
            ok = path.exists() and path.is_file()
            return ok, f"file_exists({raw_pattern}): {ok}", None

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
    """Return a stdout/stderr excerpt for failed shell checks.

    Preserves newlines (pytest/ruff/mypy output is structured) and uses
    head+tail clipping so both the setup banner AND the final FAILED
    summary survive the budget.
    """
    parts: list[str] = []
    if result.stdout.strip():
        parts.append(
            "stdout:\n"
            + clip_head_tail(result.stdout, SHELL_OUTPUT_SECTION_CHARS)
        )
    if result.stderr.strip():
        parts.append(
            "stderr:\n"
            + clip_head_tail(result.stderr, SHELL_OUTPUT_SECTION_CHARS)
        )
    return "\n".join(parts)
