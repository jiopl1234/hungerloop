"""Unit tests for built-in tool implementations (PRD §9.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.tools import (
    PatchFileTool,
    ReadFileTool,
    RunShellTool,
    WriteFileTool,
    default_tool_registry,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


async def test_read_file_returns_content(workspace: Path) -> None:
    (workspace / "data.txt").write_text("hello world", encoding="utf-8")
    outcome = await ReadFileTool().run(
        args={"path": "data.txt"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is True
    assert "hello world" in outcome.result_summary


async def test_read_file_missing_returns_failure(workspace: Path) -> None:
    outcome = await ReadFileTool().run(
        args={"path": "missing.txt"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "not_found"


async def test_read_file_rejects_path_escape(workspace: Path) -> None:
    with pytest.raises(PermissionError):
        await ReadFileTool().run(
            args={"path": "../etc/passwd"},
            workspace_root=workspace,
            task_id="t1",
            loop_id=1,
        )


async def test_write_file_creates_parents(workspace: Path) -> None:
    outcome = await WriteFileTool().run(
        args={"path": "sub/dir/report.md", "content": "hi"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is True
    assert outcome.artifact_type == "file_write"
    assert outcome.artifact_path == "sub/dir/report.md"
    assert (workspace / "sub" / "dir" / "report.md").read_text() == "hi"


async def test_patch_file_replaces_unique_match(workspace: Path) -> None:
    (workspace / "code.py").write_text("foo\nbar\nbaz\n", encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={"path": "code.py", "old_text": "bar", "new_text": "BAR"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is True
    assert outcome.artifact_type == "file_patch"
    assert (workspace / "code.py").read_text() == "foo\nBAR\nbaz\n"


async def test_patch_file_rejects_ambiguous_match(workspace: Path) -> None:
    (workspace / "code.py").write_text("x\nx\n", encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={"path": "code.py", "old_text": "x", "new_text": "y"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary.startswith("ambiguous:")


async def test_patch_file_rejects_no_match(workspace: Path) -> None:
    (workspace / "code.py").write_text("foo\n", encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={"path": "code.py", "old_text": "bar", "new_text": "y"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "no_match"


async def test_patch_file_missing_target(workspace: Path) -> None:
    outcome = await PatchFileTool().run(
        args={"path": "missing.py", "old_text": "a", "new_text": "b"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "not_found"


async def test_run_shell_argv_only(workspace: Path) -> None:
    repo = InMemoryRepository()
    sandbox = SandboxRunner(repo)
    tool = RunShellTool(sandbox)
    outcome = await tool.run(
        args={"argv": ["echo", "hello"], "timeout": 5},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is True
    assert outcome.sandbox_result is not None
    assert outcome.sandbox_result.exit_code == 0
    assert "hello" in outcome.sandbox_result.stdout


async def test_run_shell_rejects_missing_argv(workspace: Path) -> None:
    repo = InMemoryRepository()
    tool = RunShellTool(SandboxRunner(repo))
    outcome = await tool.run(
        args={"timeout": 5},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "bad_args"


def test_default_registry_has_four_tools() -> None:
    repo = InMemoryRepository()
    registry = default_tool_registry(SandboxRunner(repo))
    assert set(registry.keys()) == {
        "read_file",
        "write_file",
        "patch_file",
        "run_shell",
    }
    assert registry["run_shell"].side_effect_level == "shell"
    assert registry["write_file"].side_effect_level == "file_write"
    assert registry["patch_file"].side_effect_level == "file_write"
    assert registry["read_file"].side_effect_level == "read"
