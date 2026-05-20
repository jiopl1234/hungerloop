"""Deterministic predicates for v0.6 user-testing assertions."""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationAssertionStatus,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.path_safety import resolve_workspace_path
from hungerloop.services.sandbox_runner import SandboxRunner


class MalformedPredicateParams(ValueError):
    """Raised when a user-testing assertion has malformed predicate params."""


@dataclass(frozen=True)
class UserTestingPredicateContext:
    """Inputs available to a deterministic user-testing predicate."""

    task_id: str
    loop_id: int
    candidate: CandidateState
    assertion: ValidationAssertion
    candidate_root: Path
    repo: RepositoryProtocol
    sandbox_runner: SandboxRunner
    default_timeout_seconds: int = 60


@dataclass(frozen=True)
class UserTestingPredicateResult:
    """Outcome from one deterministic user-testing predicate invocation."""

    status: ValidationAssertionStatus
    detail: str
    evidence_payload: dict[str, object] = field(default_factory=dict)
    missing_evidence: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    timed_out: bool = False
    argv: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


UserTestingPredicate = Callable[
    [UserTestingPredicateContext],
    Awaitable[UserTestingPredicateResult],
]

_REGISTRY: dict[str, UserTestingPredicate] = {}


def register_user_testing_predicate(name: str, fn: UserTestingPredicate) -> None:
    """Register or replace a deterministic user-testing predicate."""
    if not name.strip():
        raise ValueError("predicate name cannot be empty")
    _REGISTRY[name] = fn


def get_user_testing_predicate(name: str) -> UserTestingPredicate | None:
    """Return the predicate registered for ``name``, if any."""
    return _REGISTRY.get(name)


async def behavioral_assertion(
    context: UserTestingPredicateContext,
) -> UserTestingPredicateResult:
    """Verify headers, regexes, and content snippets in one workspace file."""
    params: Mapping[str, object] = context.assertion.params
    rel_path = _require_any_str(params, ("file", "path"))
    headers = _optional_str_list(params, "headers")
    regexes = _optional_str_list(params, "regexes")
    regex_value = _optional_str_list(params, "regex")
    contains = (
        _optional_str_list(params, "contains")
        + _optional_str_list(params, "content")
        + _optional_str_list(params, "contents")
    )
    all_regexes = regexes + regex_value
    if not headers and not all_regexes and not contains:
        raise MalformedPredicateParams(
            "behavioral_assertion requires headers, regexes, or contains"
        )

    file_path = _workspace_file(context.candidate_root, rel_path)
    if not file_path.is_file():
        return UserTestingPredicateResult(
            status="failed",
            detail=f"file_not_found:{rel_path}",
            evidence_payload={"path": rel_path, "exists": False},
        )

    content_text = file_path.read_text(encoding="utf-8", errors="replace")
    snippet = content_text[:200]
    for header in headers:
        if header not in content_text:
            return UserTestingPredicateResult(
                status="failed",
                detail=f"missing_header:{header}; snippet:{snippet}",
                evidence_payload={
                    "path": rel_path,
                    "missing_header": header,
                    "snippet": snippet,
                },
            )
    for pattern in all_regexes:
        try:
            matched = re.search(pattern, content_text, flags=re.MULTILINE) is not None
        except re.error as exc:
            raise MalformedPredicateParams(f"invalid regex: {pattern}") from exc
        if not matched:
            return UserTestingPredicateResult(
                status="failed",
                detail=f"regex_not_found:{pattern}; snippet:{snippet}",
                evidence_payload={
                    "path": rel_path,
                    "missing_regex": pattern,
                    "snippet": snippet,
                },
            )
    for needle in contains:
        if needle not in content_text:
            return UserTestingPredicateResult(
                status="failed",
                detail=f"content_not_found:{needle}; snippet:{snippet}",
                evidence_payload={
                    "path": rel_path,
                    "missing_content": needle,
                    "snippet": snippet,
                },
            )

    return UserTestingPredicateResult(
        status="passed",
        detail="passed",
        evidence_payload={
            "path": rel_path,
            "headers": headers,
            "regexes": all_regexes,
            "contains": contains,
        },
    )


async def file_contains_regex(
    context: UserTestingPredicateContext,
) -> UserTestingPredicateResult:
    """Verify that a workspace file contains a regular expression."""
    params: Mapping[str, object] = context.assertion.params
    rel_path = _require_any_str(params, ("path", "file"))
    pattern = _require_str(params, "regex")
    file_path = _workspace_file(context.candidate_root, rel_path)
    if not file_path.is_file():
        return UserTestingPredicateResult(
            status="failed",
            detail=f"file_not_found:{rel_path}",
            evidence_payload={"path": rel_path, "regex": pattern, "exists": False},
        )
    content_text = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        matched = re.search(pattern, content_text, flags=re.MULTILINE) is not None
    except re.error as exc:
        raise MalformedPredicateParams(f"invalid regex: {pattern}") from exc
    if matched:
        return UserTestingPredicateResult(
            status="passed",
            detail="passed",
            evidence_payload={"path": rel_path, "regex": pattern, "matched": True},
        )
    return UserTestingPredicateResult(
        status="failed",
        detail="regex_not_found",
        evidence_payload={
            "path": rel_path,
            "regex": pattern,
            "matched": False,
            "snippet": content_text[:200],
        },
    )


async def command_stdout_contains(
    context: UserTestingPredicateContext,
) -> UserTestingPredicateResult:
    """Run a sandbox command and verify stdout contains a substring."""
    params: Mapping[str, object] = context.assertion.params
    argv = _require_str_list(params, "argv")
    if not argv:
        raise MalformedPredicateParams("argv cannot be empty")
    needle = _require_str(params, "contains")
    timeout = _optional_positive_int(
        params,
        "timeout",
        default=context.default_timeout_seconds,
    )
    result = await context.sandbox_runner.run_argv(
        task_id=context.task_id,
        loop_id=context.loop_id,
        argv=list(argv),
        cwd=context.candidate_root,
        timeout=timeout,
        evidence_label=f"user_testing:{context.assertion.assertion_id}",
    )
    supporting_evidence_ids = (
        [result.evidence_id] if result.evidence_id is not None else []
    )
    payload: dict[str, object] = {
        "argv": list(argv),
        "timeout": timeout,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "supporting_evidence_ids": supporting_evidence_ids,
    }
    if result.timed_out:
        return UserTestingPredicateResult(
            status="blocked",
            detail="timeout",
            evidence_payload=payload,
            supporting_evidence_ids=supporting_evidence_ids,
            timed_out=True,
            argv=list(argv),
            stdout=result.stdout,
            stderr=result.stderr,
        )
    if result.exit_code != 0:
        return UserTestingPredicateResult(
            status="failed",
            detail=result.stderr.strip()
            or result.stdout.strip()
            or f"exit_code:{result.exit_code}",
            evidence_payload=payload,
            supporting_evidence_ids=supporting_evidence_ids,
            argv=list(argv),
            stdout=result.stdout,
            stderr=result.stderr,
        )
    if needle in result.stdout:
        return UserTestingPredicateResult(
            status="passed",
            detail="passed",
            evidence_payload={**payload, "contains": needle, "matched": True},
            supporting_evidence_ids=supporting_evidence_ids,
            argv=list(argv),
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return UserTestingPredicateResult(
        status="failed",
        detail=f"stdout_missing:{needle}",
        evidence_payload={**payload, "contains": needle, "matched": False},
        supporting_evidence_ids=supporting_evidence_ids,
        argv=list(argv),
        stdout=result.stdout,
        stderr=result.stderr,
    )


async def evidence_summary_contains(
    context: UserTestingPredicateContext,
) -> UserTestingPredicateResult:
    """Verify successful candidate evidence summaries contain a substring."""
    params: Mapping[str, object] = context.assertion.params
    needle = _require_str(params, "contains")
    candidate_evidence_ids = set(context.candidate.evidence_ids)
    rows = context.repo.list_successful_tool_call_evidence(context.task_id)
    for row in rows:
        evidence_id = _string_or_empty(row.get("evidence_id"))
        if candidate_evidence_ids and evidence_id not in candidate_evidence_ids:
            continue
        payload_raw = row.get("payload")
        if not isinstance(payload_raw, dict):
            continue
        payload: dict[str, object] = dict(payload_raw)
        summary = _evidence_payload_text(payload)
        if needle.lower() in summary.lower():
            return UserTestingPredicateResult(
                status="passed",
                detail="passed",
                evidence_payload={
                    "contains": needle,
                    "matched_evidence_id": evidence_id,
                    "summary": summary,
                },
            )
    return UserTestingPredicateResult(
        status="failed",
        detail="evidence_summary_not_found",
        evidence_payload={
            "contains": needle,
            "candidate_evidence_ids": sorted(candidate_evidence_ids),
        },
    )


async def file_count_at_least(
    context: UserTestingPredicateContext,
) -> UserTestingPredicateResult:
    """Verify a workspace glob matches at least ``min_count`` files."""
    params: Mapping[str, object] = context.assertion.params
    glob_pattern = _require_str(params, "glob")
    min_count = _require_non_negative_int(params, "min_count")
    if Path(glob_pattern).is_absolute() or ".." in Path(glob_pattern).parts:
        raise MalformedPredicateParams("glob must stay inside candidate workspace")
    matched = sorted(
        path.relative_to(context.candidate_root).as_posix()
        for path in context.candidate_root.glob(glob_pattern)
        if path.is_file()
    )
    status: ValidationAssertionStatus = (
        "passed" if len(matched) >= min_count else "failed"
    )
    return UserTestingPredicateResult(
        status=status,
        detail="passed" if status == "passed" else f"file_count:{len(matched)}<{min_count}",
        evidence_payload={
            "glob": glob_pattern,
            "min_count": min_count,
            "matched_count": len(matched),
            "matched_files": matched,
        },
    )


def _require_str(params: Mapping[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedPredicateParams(f"{key} must be a non-empty string")
    return value


def _require_any_str(params: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    raise MalformedPredicateParams(
        f"one of {', '.join(keys)} must be a non-empty string"
    )


def _require_str_list(params: Mapping[str, object], key: str) -> list[str]:
    value = params.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise MalformedPredicateParams(f"{key} must be a non-empty list[str]")
    return list(value)


def _optional_str_list(params: Mapping[str, object], key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise MalformedPredicateParams(f"{key} must be a string or list[str]")


def _require_non_negative_int(params: Mapping[str, object], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedPredicateParams(f"{key} must be a non-negative integer")
    return value


def _optional_positive_int(
    params: Mapping[str, object],
    key: str,
    *,
    default: int,
) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MalformedPredicateParams(f"{key} must be a positive integer")
    return value


def _workspace_file(candidate_root: Path, rel_path: str) -> Path:
    try:
        return resolve_workspace_path(candidate_root, rel_path)
    except (PermissionError, ValueError) as exc:
        raise MalformedPredicateParams(str(exc)) from exc


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _evidence_payload_text(payload: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in (
        "tool_name",
        "args_summary",
        "result_summary",
        "summary",
        "response_preview",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


register_user_testing_predicate("behavioral_assertion", behavioral_assertion)
register_user_testing_predicate("file_contains_regex", file_contains_regex)
register_user_testing_predicate("command_stdout_contains", command_stdout_contains)
register_user_testing_predicate("evidence_summary_contains", evidence_summary_contains)
register_user_testing_predicate("file_count_at_least", file_count_at_least)

__all__ = [
    "MalformedPredicateParams",
    "UserTestingPredicate",
    "UserTestingPredicateContext",
    "UserTestingPredicateResult",
    "_REGISTRY",
    "get_user_testing_predicate",
    "register_user_testing_predicate",
]
