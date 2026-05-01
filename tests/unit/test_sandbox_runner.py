"""Unit tests for SandboxRunner (Task 7)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.services.sandbox_runner import SandboxRunner, SandboxRunResult


def test_sandbox_run_result_model_fields() -> None:
    """SandboxRunResult is a Pydantic model with the expected fields."""
    result = SandboxRunResult(
        argv=["echo"],
        cwd="/tmp",
        exit_code=0,
        stdout="",
        stderr="",
    )
    assert result.timed_out is False
    assert result.evidence_id is None


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.save_shell_output_as_evidence.return_value = "ev-001"
    return repo


@pytest.fixture
def runner(mock_repo: MagicMock) -> SandboxRunner:
    return SandboxRunner(repo=mock_repo, max_output_chars=100)


async def test_argv_execution_success(runner: SandboxRunner, tmp_path: Path) -> None:
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["echo", "hello"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test",
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


async def test_argv_execution_failure(runner: SandboxRunner, tmp_path: Path) -> None:
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["false"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test",
    )
    assert result.exit_code != 0


async def test_timeout_returns_timed_out(runner: SandboxRunner, tmp_path: Path) -> None:
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["sleep", "30"],
        cwd=tmp_path,
        timeout=1,
        evidence_label="test",
    )
    assert result.timed_out is True


async def test_stdout_truncated(mock_repo: MagicMock, tmp_path: Path) -> None:
    runner = SandboxRunner(repo=mock_repo, max_output_chars=10)
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["python3", "-c", "print('x' * 1000)"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test",
    )
    assert len(result.stdout) <= 10


async def test_empty_argv_rejected(runner: SandboxRunner, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv cannot be empty"):
        await runner.run_argv(
            task_id="t1",
            loop_id=1,
            argv=[],
            cwd=tmp_path,
            timeout=10,
            evidence_label="test",
        )


async def test_zero_timeout_rejected(runner: SandboxRunner, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        await runner.run_argv(
            task_id="t1",
            loop_id=1,
            argv=["echo"],
            cwd=tmp_path,
            timeout=0,
            evidence_label="test",
        )


async def test_evidence_saved(runner: SandboxRunner, mock_repo: MagicMock, tmp_path: Path) -> None:
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["echo", "hi"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test:label",
    )
    assert result.evidence_id == "ev-001"
    mock_repo.save_shell_output_as_evidence.assert_called_once()


async def test_stderr_captured(runner: SandboxRunner, tmp_path: Path) -> None:
    """The runner captures stderr separately from stdout."""
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["python3", "-c", "import sys; sys.stderr.write('boom')"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test",
    )
    assert "boom" in result.stderr


async def test_nonzero_exit_keeps_stdout(runner: SandboxRunner, tmp_path: Path) -> None:
    """A non-zero exit must not blank stdout — the agent needs the output to debug."""
    result = await runner.run_argv(
        task_id="t1",
        loop_id=1,
        argv=["python3", "-c", "print('partial output'); import sys; sys.exit(2)"],
        cwd=tmp_path,
        timeout=10,
        evidence_label="test",
    )
    assert result.exit_code == 2
    assert "partial output" in result.stdout


async def test_missing_cwd_rejected(runner: SandboxRunner, tmp_path: Path) -> None:
    """A non-existent cwd is rejected with a clear ValueError before subprocess spawn."""
    with pytest.raises(ValueError, match="cwd does not exist"):
        await runner.run_argv(
            task_id="t1",
            loop_id=1,
            argv=["echo", "hi"],
            cwd=tmp_path / "does-not-exist",
            timeout=10,
            evidence_label="test",
        )
