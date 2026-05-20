from __future__ import annotations

from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.validation_contract import ValidationAssertion
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.sandbox_runner import SandboxRunResult
from hungerloop.services.validators.user_testing_predicates import (
    _REGISTRY,
    UserTestingPredicateContext,
    UserTestingPredicateResult,
    get_user_testing_predicate,
    register_user_testing_predicate,
)

TASK_ID = "task-1"
LOOP_ID = 3


class _RecordingSandboxRunner:
    def __init__(self, results: list[SandboxRunResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, object]] = []

    async def run_argv(
        self,
        task_id: str,
        loop_id: int,
        argv: list[str],
        cwd: Path,
        timeout: int,
        evidence_label: str,
    ) -> SandboxRunResult:
        self.calls.append(
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "argv": list(argv),
                "cwd": cwd,
                "timeout": timeout,
                "evidence_label": evidence_label,
            }
        )
        result = self.results.pop(0)
        return result.model_copy(update={"argv": list(argv), "cwd": str(cwd)})


def _sandbox_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
    evidence_id: str = "shell-ev",
) -> SandboxRunResult:
    return SandboxRunResult(
        argv=[],
        cwd="",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        evidence_id=evidence_id,
    )


def _candidate(evidence_ids: list[str] | None = None) -> CandidateState:
    return CandidateState(
        id="CAND-task-1-3",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate evidence summary",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=list(evidence_ids or []),
    )


def _assertion(
    check_type: str,
    params: dict[str, object],
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=f"ASSERT-{check_type}",
        phase_id="phase-1",
        title=check_type,
        description=check_type,
        check_type=check_type,
        params=params,
    )


def _context(
    *,
    tmp_path: Path,
    assertion: ValidationAssertion,
    repo: InMemoryRepository | None = None,
    sandbox: _RecordingSandboxRunner | None = None,
    candidate: CandidateState | None = None,
) -> UserTestingPredicateContext:
    return UserTestingPredicateContext(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        candidate=candidate or _candidate(),
        assertion=assertion,
        candidate_root=tmp_path,
        repo=repo or InMemoryRepository(),
        sandbox_runner=sandbox or _RecordingSandboxRunner(),
        default_timeout_seconds=60,
    )


async def test_behavioral_assertion_registered(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("# Title\n\n## Foo\nbody\n", encoding="utf-8")
    assertion = _assertion(
        "behavioral_assertion",
        {"file": "x.md", "headers": ["## Foo"]},
    )

    assert callable(_REGISTRY["behavioral_assertion"])
    result = await get_user_testing_predicate("behavioral_assertion")(
        _context(tmp_path=tmp_path, assertion=assertion)
    )

    assert result.status == "passed"
    assert result.detail == "passed"


async def test_file_contains_regex(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("hello   world", encoding="utf-8")

    passed = await get_user_testing_predicate("file_contains_regex")(
        _context(
            tmp_path=tmp_path,
            assertion=_assertion(
                "file_contains_regex",
                {"path": "report.md", "regex": r"hello\s+world"},
            ),
        )
    )
    failed = await get_user_testing_predicate("file_contains_regex")(
        _context(
            tmp_path=tmp_path,
            assertion=_assertion(
                "file_contains_regex",
                {"path": "report.md", "regex": "goodbye"},
            ),
        )
    )

    assert passed.status == "passed"
    assert failed.status == "failed"
    assert failed.detail == "regex_not_found"


async def test_command_stdout_contains(tmp_path: Path) -> None:
    sandbox = _RecordingSandboxRunner(
        [_sandbox_result(stdout="hello from sandbox", evidence_id="shell-ev")]
    )
    assertion = _assertion(
        "command_stdout_contains",
        {"argv": ["python", "-c", "print('hello')"], "contains": "hello"},
    )

    result = await get_user_testing_predicate("command_stdout_contains")(
        _context(tmp_path=tmp_path, assertion=assertion, sandbox=sandbox)
    )

    assert result.status == "passed"
    assert result.supporting_evidence_ids == ["shell-ev"]
    assert sandbox.calls == [
        {
            "task_id": TASK_ID,
            "loop_id": LOOP_ID,
            "argv": ["python", "-c", "print('hello')"],
            "cwd": tmp_path,
            "timeout": 60,
            "evidence_label": "user_testing:ASSERT-command_stdout_contains",
        }
    ]


async def test_evidence_summary_contains(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    repo.create_task(TASK_ID, "goal")
    evidence_id = repo.save_tool_call_as_evidence(
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        agent_id="worker",
        tool_name="write_file",
        args_summary="wrote report",
        result_summary="Generated mission report artifact",
        success=True,
        elapsed_ms=5,
    )

    result = await get_user_testing_predicate("evidence_summary_contains")(
        _context(
            tmp_path=tmp_path,
            repo=repo,
            candidate=_candidate([evidence_id]),
            assertion=_assertion(
                "evidence_summary_contains",
                {"contains": "mission report"},
            ),
        )
    )

    assert result.status == "passed"
    assert result.evidence_payload["matched_evidence_id"] == evidence_id


async def test_file_count_at_least(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    result = await get_user_testing_predicate("file_count_at_least")(
        _context(
            tmp_path=tmp_path,
            assertion=_assertion(
                "file_count_at_least",
                {"glob": "*.py", "min_count": 2},
            ),
        )
    )

    assert result.status == "passed"
    assert result.evidence_payload["matched_count"] == 2


async def test_register_predicate(tmp_path: Path) -> None:
    async def my_predicate(
        context: UserTestingPredicateContext,
    ) -> UserTestingPredicateResult:
        return UserTestingPredicateResult(
            status="passed",
            detail=f"custom:{context.assertion.assertion_id}",
        )

    register_user_testing_predicate("my_check_for_unit_test", my_predicate)
    result = await get_user_testing_predicate("my_check_for_unit_test")(
        _context(
            tmp_path=tmp_path,
            assertion=_assertion("my_check_for_unit_test", {}),
        )
    )

    assert result.status == "passed"
    assert result.detail == "custom:ASSERT-my_check_for_unit_test"
