"""Deterministic check proposal gate for v0.7 spec-to-check synthesis.

The gate is responsible for:
1. Deduplicating proposals against existing proposal/check keys.
2. Validating proposal shape (argv, path safety).
3. Enforcing an argv allowlist (defaulting to ``python``).
4. Validating relative paths for ``FILE_EXISTS`` proposals.
5. Dry-running ``SHELL_EXIT_ZERO`` proposals **twice** through a
   ``DryRunner`` adapter and requiring identical pass/fail outcome.
6. Returning accepted and rejected proposals with stable reason codes.

The gate is deterministic and never calls LLMs.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal, _normalize_executable

if TYPE_CHECKING:
    from hungerloop.services.sandbox_runner import SandboxRunner

# Default allowlist of executable names (already normalized).
DEFAULT_ALLOWLIST: list[str] = ["python"]

# Reason codes (stable strings).
REASON_DUPLICATE = "duplicate"
REASON_ABSOLUTE_PATH = "unsafe_path:absolute"
REASON_PATH_TRAVERSAL = "unsafe_path:traversal"
REASON_NUL_IN_PATH = "unsafe_path:nul_byte"
REASON_MISSING_ARGV = "invalid_argv:missing"
REASON_INVALID_ARGV_ELEMENT = "invalid_argv:non_string_or_empty"
REASON_NON_ALLOWLISTED = "non_allowlisted_command"
REASON_NONDETERMINISTIC = "nondeterministic"
REASON_MISSING_FIXTURE = "missing_fixture"
REASON_FIXTURE_SETUP_FAILED = "fixture_setup_failed"
REASON_FIXTURE_NONDETERMINISTIC = "fixture_nondeterministic"
REASON_ASSERTION_NOT_EXECUTABLE = "assertion_not_executable"

DryRunFailureKind = Literal["syntax_error", "setup_error", "timeout"]


class DryRunResult(BaseModel):
    """Structured dry-run result that distinguishes invalid checks from failures."""

    passed: bool
    failure_kind: DryRunFailureKind | None = None
    stderr_excerpt: str = ""


def _coerce_dry_run_result(result: bool | DryRunResult) -> DryRunResult:
    if isinstance(result, bool):
        return DryRunResult(passed=result)
    return result


def _assertion_not_executable_reason(result: DryRunResult) -> str | None:
    if result.failure_kind is None:
        return None
    return f"{REASON_ASSERTION_NOT_EXECUTABLE}:{result.failure_kind}"


def _classify_sandbox_failure(
    *,
    stderr: str,
    exit_code: int,
    timed_out: bool,
) -> DryRunFailureKind | None:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return None
    stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    terminal_line = stderr_lines[-1] if stderr_lines else ""
    if any(
        terminal_line.startswith(f"{exception_name}:")
        for exception_name in ("SyntaxError", "IndentationError", "TabError")
    ):
        return "syntax_error"
    return None


@runtime_checkable
class DryRunner(Protocol):
    """Adapter for dry-running shell proposals.

    The stable public method returns a bool. Production adapters may also
    implement :class:`DetailedDryRunner` for diagnostic classification.
    """

    async def dry_run(
        self, argv: list[str], cwd: Path | None = None
    ) -> bool:
        """Run *argv* once and return whether it exits zero."""
        ...


@runtime_checkable
class DetailedDryRunner(Protocol):
    """Optional adapter extension exposing failure classification."""

    async def dry_run_detailed(
        self, argv: list[str], cwd: Path | None = None
    ) -> DryRunResult:
        ...


class SandboxDryRunner(DryRunner):
    """Production ``DryRunner`` that wraps :class:`SandboxRunner`.

    The adapter calls ``SandboxRunner.run_argv`` with a minimal timeout
    and output cap, a fixed task/loop pair for evidence tagging, and the
    sandbox boundary required by invariant I-7.  It returns ``True``
    when the process exits zero and ``False`` for any non-zero exit,
    timeout, or execution error.

    The fixed task/loop identifiers are ``__dry_run__`` / ``0`` so that
    dry-run evidence is distinguishable from real validation evidence.
    """

    def __init__(
        self,
        sandbox: SandboxRunner,
        *,
        timeout: int = 30,
        cwd: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._cwd = cwd or Path.cwd()

    async def dry_run_detailed(
        self, argv: list[str], cwd: Path | None = None
    ) -> DryRunResult:
        """Run *argv* once and classify high-confidence execution failures."""
        run_cwd = cwd if cwd is not None else self._cwd
        isolated_root: Path | None = None
        try:
            if not run_cwd.is_dir():
                return DryRunResult(passed=False, failure_kind="setup_error")
            isolated_root = Path(
                tempfile.mkdtemp(
                    prefix=".hungerloop-dry-run-",
                    dir=run_cwd.parent,
                )
            )
            isolated_cwd = isolated_root / "files"
            shutil.copytree(run_cwd, isolated_cwd)
            result = await self._sandbox.run_argv(
                task_id="__dry_run__",
                loop_id=0,
                argv=list(argv),
                cwd=isolated_cwd,
                timeout=self._timeout,
                evidence_label="check_proposal_dry_run",
            )
            return DryRunResult(
                passed=result.exit_code == 0 and not result.timed_out,
                failure_kind=_classify_sandbox_failure(
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                ),
                stderr_excerpt=result.stderr[:1000],
            )
        except (ValueError, FileNotFoundError, PermissionError, OSError) as exc:
            return DryRunResult(
                passed=False,
                failure_kind="setup_error",
                stderr_excerpt=f"{type(exc).__name__}: {exc}"[:1000],
            )
        finally:
            if isolated_root is not None:
                shutil.rmtree(isolated_root, ignore_errors=True)

    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        """Compatibility API returning only the process success boolean."""
        return (
            await self.dry_run_detailed(argv, cwd=cwd)
        ).passed


class RejectedProposal(BaseModel):
    """A proposal that was rejected by the gate, with a stable reason."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    proposal: CheckProposal
    reason: str
    dedup_key: str


class GateResult(BaseModel):
    """Result of filtering proposals through the gate."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    accepted: list[CheckProposal] = Field(default_factory=list)
    rejected: list[RejectedProposal] = Field(default_factory=list)


def _is_absolute_path(path: str) -> bool:
    """Check if a path is absolute on any platform."""
    # POSIX absolute path (starts with /)
    if path.startswith("/") or path.startswith("\\"):
        return True
    # Windows absolute path (C:\, D:\, etc.)
    if len(path) >= 2 and path[1] == ":":
        return True
    return False


def _has_traversal(path: str) -> bool:
    """Check if a path contains directory traversal (``..``)."""
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    return ".." in parts


def _has_nul(path: str) -> bool:
    """Check if a path contains a NUL byte."""
    return "\x00" in path


def _is_unsafe_path(path: str) -> str | None:
    """Return a reason code if the path is unsafe, else ``None``."""
    if _has_nul(path):
        return REASON_NUL_IN_PATH
    if _is_absolute_path(path):
        return REASON_ABSOLUTE_PATH
    if _has_traversal(path):
        return REASON_PATH_TRAVERSAL
    return None


def _validate_argv(argv: object, allowlist: list[str]) -> str | None:
    """Validate argv at gate level.  Returns a reason code or ``None``.

    The model already validates argv shape at construction time, but the
    gate also handles proposals whose ``params`` were tampered with after
    construction (defence-in-depth).
    """
    if not isinstance(argv, list):
        return REASON_MISSING_ARGV
    if len(argv) == 0:
        return REASON_MISSING_ARGV
    for elem in argv:
        if not isinstance(elem, str) or not elem.strip():
            return REASON_INVALID_ARGV_ELEMENT
    head = _normalize_executable(argv[0])
    if head not in allowlist:
        return REASON_NON_ALLOWLISTED
    return None


def _python_inline_syntax_reason(argv: object) -> str | None:
    """Statically reject malformed allowlisted ``python -c`` snippets."""
    if not isinstance(argv, list) or len(argv) < 3:
        return None
    if not isinstance(argv[0], str) or _normalize_executable(argv[0]) != "python":
        return None
    if argv[1] != "-c" or not isinstance(argv[2], str):
        return None
    try:
        compile(argv[2], "<synthesized-check>", "exec")
    except (SyntaxError, ValueError, TypeError):
        return f"{REASON_ASSERTION_NOT_EXECUTABLE}:syntax_error"
    return None


class CheckProposalGate:
    """Deterministic proposal gate.

    The gate never calls LLMs and never writes to the ledger.  It accepts
    a ``DryRunner`` adapter for shell proposals and an optional allowlist
    of executable names.
    """

    def __init__(
        self,
        *,
        dry_runner: DryRunner | None = None,
        allowlist: list[str] | None = None,
    ) -> None:
        self._dry_runner = dry_runner
        # Normalize allowlist entries at construction time.
        if allowlist is not None:
            self._allowlist = [_normalize_executable(a) for a in allowlist]
        else:
            self._allowlist = list(DEFAULT_ALLOWLIST)

    async def filter(
        self,
        proposals: list[CheckProposal],
        *,
        existing_keys: set[str] | None = None,
        dry_run_cwd: Path | None = None,
        require_fixture: bool = False,
        defer_fixture_precheck: bool = False,
    ) -> GateResult:
        """Filter proposals through the gate.

        Args:
            proposals: List of proposals to filter.
            existing_keys: Optional set of dedup keys already known to
                the system (from existing checks, prior proposals, etc.).

        Returns:
            ``GateResult`` with accepted and rejected proposals.
        """
        known: set[str] = set(existing_keys) if existing_keys else set()
        accepted: list[CheckProposal] = []
        rejected: list[RejectedProposal] = []

        def _reject(proposal: CheckProposal, reason: str, key: str) -> None:
            """Append a RejectedProposal without re-validating the proposal."""
            rejected.append(
                RejectedProposal.model_construct(
                    proposal=proposal, reason=reason, dedup_key=key
                )
            )
            known.add(key)

        for proposal in proposals:
            key = proposal.dedup_key()

            # 1. Duplicate check
            if key in known:
                _reject(proposal, REASON_DUPLICATE, key)
                continue

            # 2. Type-specific validation
            if proposal.check_type == AcceptanceCheckType.FILE_EXISTS:
                reason = self._validate_file_proposal(proposal)
                if reason is not None:
                    _reject(proposal, reason, key)
                    continue
                # file_exists proposals are accepted without dry-run
                accepted.append(proposal)
                known.add(key)

            elif proposal.check_type == AcceptanceCheckType.SHELL_EXIT_ZERO:
                reason = self._validate_shell_proposal(proposal)
                if reason is not None:
                    _reject(proposal, reason, key)
                    continue
                syntax_reason = _python_inline_syntax_reason(
                    proposal.params.get("argv")
                )
                if syntax_reason is not None:
                    _reject(proposal, syntax_reason, key)
                    continue
                if require_fixture and proposal.fixture_argv is None:
                    _reject(proposal, REASON_MISSING_FIXTURE, key)
                    continue
                if proposal.fixture_argv is not None:
                    fixture_reason = _validate_argv(
                        proposal.fixture_argv,
                        self._allowlist,
                    )
                    if fixture_reason is not None:
                        _reject(proposal, fixture_reason, key)
                        continue
                    if _python_inline_syntax_reason(proposal.fixture_argv) is not None:
                        _reject(proposal, REASON_FIXTURE_SETUP_FAILED, key)
                        continue
                if defer_fixture_precheck:
                    accepted.append(proposal)
                    known.add(key)
                    continue
                if proposal.fixture_argv is not None:
                    fixture_reason = await self._dry_run_fixture(
                        proposal.fixture_argv,
                        cwd=dry_run_cwd,
                    )
                    if fixture_reason is not None:
                        _reject(proposal, fixture_reason, key)
                        continue
                # Dry-run twice and require identical outcome
                if proposal.fixture_argv is not None:
                    dry_run_reason = await self._dry_run_composite_check(
                        fixture_argv=proposal.fixture_argv,
                        assertion_argv=list(proposal.params["argv"]),
                        cwd=dry_run_cwd,
                    )
                else:
                    dry_run_reason = await self._dry_run_check(
                        proposal, cwd=dry_run_cwd
                    )
                if dry_run_reason is not None:
                    _reject(proposal, dry_run_reason, key)
                    continue
                accepted.append(proposal)
                known.add(key)
            else:
                # Should not reach here due to model validation, but
                # reject defensively.
                _reject(proposal, "unsupported_check_type", key)

        return GateResult(accepted=accepted, rejected=rejected)

    def _validate_file_proposal(self, proposal: CheckProposal) -> str | None:
        """Validate a file_exists proposal.  Returns reason or ``None``."""
        path = proposal.params.get("path")
        if not isinstance(path, str):
            return REASON_INVALID_ARGV_ELEMENT
        unsafe_reason = _is_unsafe_path(path)
        if unsafe_reason is not None:
            return unsafe_reason
        return None

    def _validate_shell_proposal(self, proposal: CheckProposal) -> str | None:
        """Validate a shell_exit_zero proposal.  Returns reason or ``None``."""
        argv = proposal.params.get("argv")
        return _validate_argv(argv, self._allowlist)

    async def _run_dry_once(
        self,
        argv: list[str],
        *,
        cwd: Path | None,
    ) -> DryRunResult:
        """Use diagnostics when available while preserving bool-only fakes."""
        if self._dry_runner is None:
            return DryRunResult(passed=False, failure_kind="setup_error")
        if isinstance(self._dry_runner, DetailedDryRunner):
            return await self._dry_runner.dry_run_detailed(argv, cwd=cwd)
        return _coerce_dry_run_result(
            await self._dry_runner.dry_run(argv, cwd=cwd)
        )

    async def _dry_run_check(
        self, proposal: CheckProposal, *, cwd: Path | None = None
    ) -> str | None:
        """Dry-run the proposal twice and check for determinism.

        Returns ``None`` if accepted, or a reason code if rejected.
        """
        if self._dry_runner is None:
            # No dry runner configured: cannot verify determinism.
            # Reject to fail closed.
            return "no_dry_runner"

        argv = proposal.params.get("argv", [])
        assert isinstance(argv, list)

        result1 = await self._run_dry_once(list(argv), cwd=cwd)
        non_executable = _assertion_not_executable_reason(result1)
        if non_executable is not None:
            return non_executable
        result2 = await self._run_dry_once(list(argv), cwd=cwd)
        non_executable = _assertion_not_executable_reason(result2)
        if non_executable is not None:
            return non_executable
        if result1.passed != result2.passed:
            return REASON_NONDETERMINISTIC
        return None

    async def _dry_run_fixture(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
    ) -> str | None:
        """Require fixture setup to succeed deterministically twice."""
        if self._dry_runner is None:
            return "no_dry_runner"

        result1 = await self._run_dry_once(list(argv), cwd=cwd)
        result2 = await self._run_dry_once(list(argv), cwd=cwd)
        if result1.passed != result2.passed or result1.failure_kind != result2.failure_kind:
            return REASON_FIXTURE_NONDETERMINISTIC
        if not result1.passed:
            return REASON_FIXTURE_SETUP_FAILED
        return None

    async def _dry_run_composite_check(
        self,
        *,
        fixture_argv: list[str],
        assertion_argv: list[str],
        cwd: Path | None = None,
    ) -> str | None:
        """Run fixture then assertion twice from a restored workspace snapshot."""
        if self._dry_runner is None:
            return "no_dry_runner"
        script = (
            "import json, subprocess, sys\n"
            "for command in json.loads(sys.argv[1]):\n"
            "    result = subprocess.run(command)\n"
            "    if result.returncode:\n"
            "        raise SystemExit(result.returncode)\n"
        )
        composite_argv = [
            "python",
            "-c",
            script,
            json.dumps([fixture_argv, assertion_argv]),
        ]
        result1 = await self._run_dry_once(composite_argv, cwd=cwd)
        non_executable = _assertion_not_executable_reason(result1)
        if non_executable is not None:
            return non_executable
        result2 = await self._run_dry_once(composite_argv, cwd=cwd)
        non_executable = _assertion_not_executable_reason(result2)
        if non_executable is not None:
            return non_executable
        if result1.passed != result2.passed:
            return REASON_NONDETERMINISTIC
        return None
