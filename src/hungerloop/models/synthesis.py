"""Check proposal model for v0.7 spec-to-check synthesis.

``CheckProposal`` is the shared model for both LLM-generated and
worker-generated deterministic acceptance checks.  Only
``SHELL_EXIT_ZERO`` and ``FILE_EXISTS`` proposal types are allowed;
non-deterministic check types (``LLM_JUDGE``, ``HUMAN_APPROVAL``, etc.)
are rejected at model-construction time.

The ``dedup_key()`` method produces a stable, behavior-based key so
that equivalent proposals can be deduplicated without over-normalizing
semantically meaningful argument values.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hungerloop.models.enums import AcceptanceCheckType

# Check types that are allowed for proposals.
_ALLOWED_PROPOSAL_TYPES: frozenset[AcceptanceCheckType] = frozenset(
    {
        AcceptanceCheckType.SHELL_EXIT_ZERO,
        AcceptanceCheckType.FILE_EXISTS,
    }
)

# Pattern to strip version suffixes from Python executables.
# Matches: python3, python3.11, python3.exe, python3.11.exe, etc.
_PY_VERSION_RE = re.compile(r"(\d+(\.\d+)*)?(\.exe)?$", re.IGNORECASE)


def _normalize_executable(name: str) -> str:
    """Normalize a Python executable name to its canonical form.

    ``python``, ``python3``, ``python3.11``, ``python.exe``,
    ``python3.exe`` all normalize to ``python``.
    Other executables are lowercased and have ``.exe`` stripped.
    """
    stripped = name.strip()
    # Remove .exe suffix (case-insensitive)
    if stripped.lower().endswith(".exe"):
        stripped = stripped[: -len(".exe")]
    # Check if this is a python variant
    lower = stripped.lower()
    if lower.startswith("python"):
        return "python"
    return lower


def _normalize_path(path: str) -> str:
    """Normalize a file path for deduplication.

    * Backslashes become forward slashes.
    * Leading ``./`` is stripped.
    * Consecutive slashes are collapsed.
    * Trailing slashes are removed.

    Semantic path components (case, names) are preserved.
    """
    normalized = path.replace("\\", "/")
    # Strip leading ./
    while normalized.startswith("./"):
        normalized = normalized[2:]
    # Collapse consecutive slashes
    normalized = re.sub(r"/+", "/", normalized)
    # Strip trailing slash
    if normalized.endswith("/") and len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


class CheckProposal(BaseModel):
    """A proposed deterministic acceptance check.

    Fields:
        check_type: Must be ``SHELL_EXIT_ZERO`` or ``FILE_EXISTS``.
        params: Dictionary with check-specific parameters.
            For ``SHELL_EXIT_ZERO``: ``{"argv": ["python", "-m", "pytest"]}``
            For ``FILE_EXISTS``: ``{"path": "src/hungerloop/main.py"}``
        description: Human-readable description of the check.
        source_quote: Non-empty quote from the spec that anchors the proposal.
        proposed_by: Identifier of the proposer (e.g. ``"synthesizer"``,
            ``"worker:agent-1"``).
    """

    model_config = ConfigDict(extra="forbid")

    check_type: AcceptanceCheckType
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    source_quote: str
    proposed_by: str = "unknown"

    @field_validator("check_type")
    @classmethod
    def _validate_check_type(cls, v: AcceptanceCheckType) -> AcceptanceCheckType:
        if v not in _ALLOWED_PROPOSAL_TYPES:
            allowed = ", ".join(t.value for t in _ALLOWED_PROPOSAL_TYPES)
            raise ValueError(
                f"CheckProposal only allows deterministic check types "
                f"({allowed}); got {v.value}"
            )
        return v

    @field_validator("source_quote")
    @classmethod
    def _validate_source_quote(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_quote must be a non-empty string")
        return v

    @field_validator("params")
    @classmethod
    def _validate_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("params must be a dictionary")
        return v

    @model_validator(mode="after")
    def _validate_params_for_type(self) -> CheckProposal:
        if self.check_type == AcceptanceCheckType.SHELL_EXIT_ZERO:
            argv = self.params.get("argv")
            if argv is None:
                raise ValueError(
                    "SHELL_EXIT_ZERO proposals require an 'argv' key in params"
                )
            if not isinstance(argv, list):
                raise ValueError("'argv' must be a list of strings")
            if len(argv) == 0:
                raise ValueError("'argv' must not be empty")
            for elem in argv:
                if not isinstance(elem, str):
                    raise ValueError("All argv elements must be strings")
                if not elem.strip():
                    raise ValueError("argv elements must not be empty or whitespace-only")
        elif self.check_type == AcceptanceCheckType.FILE_EXISTS:
            path = self.params.get("path")
            if path is None:
                raise ValueError(
                    "FILE_EXISTS proposals require a 'path' key in params"
                )
            if not isinstance(path, str):
                raise ValueError("'path' must be a string")
            if not path.strip():
                raise ValueError("'path' must not be empty")
        return self

    def dedup_key(self) -> str:
        """Compute a stable, behavior-based deduplication key.

        Equivalent proposals that differ only by:
        - Platform-normalized executable (python3 -> python, python.exe -> python)
        - Argv whitespace (leading/trailing spaces stripped)
        - Path normalization (backslashes, ./, double slashes)
        - Description text

        produce the same key.

        Semantic argument values remain case-sensitive:
        ``-q`` and ``-Q`` produce different keys.
        """
        return compute_dedup_key(
            check_type=self.check_type,
            params=self.params,
        )


def compute_dedup_key(
    *,
    check_type: AcceptanceCheckType,
    params: dict[str, Any],
) -> str:
    """Compute a stable, behavior-based deduplication key.

    This is the **single source of truth** for dedup key construction,
    shared by :meth:`CheckProposal.dedup_key` and the compiler's
    :func:`refinement_compiler._check_dedup_key`.  Both call sites must
    route through this function to avoid normalization divergence.

    Equivalent proposals that differ only by:
    - Platform-normalized executable (python3 -> python, python.exe -> python)
    - Argv whitespace (leading/trailing spaces stripped)
    - Path normalization (backslashes, ./, double slashes)
    - Description text

    produce the same key.

    Semantic argument values remain case-sensitive:
    ``-q`` and ``-Q`` produce different keys.
    """
    if check_type == AcceptanceCheckType.SHELL_EXIT_ZERO:
        argv = params.get("argv", [])
        if not isinstance(argv, list) or len(argv) == 0:
            return "shell_exit_zero:invalid_argv"
        normalized: list[str] = []
        for i, elem in enumerate(argv):
            if not isinstance(elem, str) or not elem.strip():
                return "shell_exit_zero:invalid_argv"
            if i == 0:
                normalized.append(_normalize_executable(elem))
            else:
                normalized.append(elem.strip())
        return f"shell_exit_zero:|{'|'.join(normalized)}"
    elif check_type == AcceptanceCheckType.FILE_EXISTS:
        path = params.get("path", "")
        if not isinstance(path, str):
            return "file_exists:invalid_path"
        return f"file_exists:{_normalize_path(path)}"
    # Should never reach here due to model validation, but satisfy mypy
    return f"{check_type.value}:{params!r}"
