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

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal, _normalize_executable

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


@runtime_checkable
class DryRunner(Protocol):
    """Adapter for dry-running shell proposals.

    Implementations wrap ``SandboxRunner`` (or a fake in tests) and return
    ``True`` when the command exits zero, ``False`` otherwise.
    """

    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        """Run *argv* once and return whether exit code is 0."""
        ...


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
                # Dry-run twice and require identical outcome
                dry_run_reason = await self._dry_run_check(proposal)
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

    async def _dry_run_check(self, proposal: CheckProposal) -> str | None:
        """Dry-run the proposal twice and check for determinism.

        Returns ``None`` if accepted, or a reason code if rejected.
        """
        if self._dry_runner is None:
            # No dry runner configured: cannot verify determinism.
            # Reject to fail closed.
            return "no_dry_runner"

        argv = proposal.params.get("argv", [])
        assert isinstance(argv, list)

        result1 = await self._dry_runner.dry_run(list(argv))
        result2 = await self._dry_runner.dry_run(list(argv))

        if result1 != result2:
            return REASON_NONDETERMINISTIC
        return None
