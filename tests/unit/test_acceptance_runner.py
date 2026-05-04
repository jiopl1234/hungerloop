"""Edge-case unit tests for AcceptanceCheckRunner.

The orchestrator integration tests + ValidationGate tests mock this
runner via MagicMock(spec=AcceptanceCheckRunner), so the real dispatch
logic was never exercised before. These tests pin down each branch.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.workspace_manager import WorkspaceManager


class _DummyCheck:
    def __init__(self, check_type: AcceptanceCheckType, params: dict[str, object]) -> None:
        self.check_type = check_type
        self.params = params


class _DummyCandidate:
    id = "C-1"
    evidence_ids: list[str] = []
    artifact_ids: list[str] = []


@pytest.fixture
def runner_setup(
    tmp_path: Path,
) -> tuple[AcceptanceCheckRunner, WorkspaceManager]:
    repo = InMemoryRepository()
    wm = WorkspaceManager(tmp_path)
    wm.ensure_task_workspace("t1")
    wm.create_candidate_workspace("t1", 1)
    sb = SandboxRunner(repo)
    return AcceptanceCheckRunner(repo, wm, sb), wm


async def test_llm_judge_raises_not_implemented(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    runner, _ = runner_setup
    check = _DummyCheck(AcceptanceCheckType.LLM_JUDGE, {})
    with pytest.raises(NotImplementedError, match="LLM_JUDGE"):
        await runner.run(check, "t1", 1, _DummyCandidate())


async def test_shell_exit_zero_missing_argv_raises(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    runner, _ = runner_setup
    check = _DummyCheck(AcceptanceCheckType.SHELL_EXIT_ZERO, {"timeout": 10})
    with pytest.raises(ValueError, match="argv"):
        await runner.run(check, "t1", 1, _DummyCandidate())


async def test_evidence_count_zero_minimum_passes_with_no_evidence(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    """min_count=0 always passes — zero evidence is enough zero evidence."""
    runner, _ = runner_setup
    check = _DummyCheck(
        AcceptanceCheckType.EVIDENCE_COUNT_MIN,
        {"evidence_type": "any", "min_count": 0},
    )
    passed, _, ev_id = await runner.run(check, "t1", 1, _DummyCandidate())
    assert passed is True
    assert ev_id is None


async def test_human_approval_unknown_id_returns_false_not_raises(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    """An approval that was never granted must surface as passed=False,
    not a KeyError or NotImplementedError."""
    runner, _ = runner_setup
    check = _DummyCheck(
        AcceptanceCheckType.HUMAN_APPROVAL,
        {"approval_id": "never-granted"},
    )
    passed, _, ev_id = await runner.run(check, "t1", 1, _DummyCandidate())
    assert passed is False
    assert ev_id is None


async def test_file_exists_path_escape_does_not_crash_orchestrator(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    """I-7: a path-escape in the acceptance check params must either be
    rejected loudly (PermissionError) or returned as passed=False.
    Crashing the orchestrator is unsafe — the loop would never get to
    record a tool_failed evidence row."""
    runner, _ = runner_setup
    check = _DummyCheck(
        AcceptanceCheckType.FILE_EXISTS, {"path": "../../../etc/passwd"}
    )
    try:
        passed, _, _ = await runner.run(check, "t1", 1, _DummyCandidate())
        assert passed is False
    except PermissionError:
        pass  # Loud rejection is also acceptable.


async def test_file_exists_returns_true_when_file_present(
    runner_setup: tuple[AcceptanceCheckRunner, WorkspaceManager],
) -> None:
    runner, wm = runner_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "report.md").write_text("ok")

    check = _DummyCheck(AcceptanceCheckType.FILE_EXISTS, {"path": "report.md"})
    passed, detail, ev_id = await runner.run(check, "t1", 1, _DummyCandidate())
    assert passed is True
    assert "report.md" in detail
    assert ev_id is None
