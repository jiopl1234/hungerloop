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


async def test_read_file_respects_line_offset_and_limit(workspace: Path) -> None:
    (workspace / "data.txt").write_text(
        "\n".join(f"line {index}" for index in range(1, 21)),
        encoding="utf-8",
    )

    outcome = await ReadFileTool().run(
        args={"path": "data.txt", "offset": 10, "limit": 3},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert "[lines 10-12 of 20]" in outcome.result_summary
    assert "line 10" in outcome.result_summary
    assert "line 12" in outcome.result_summary
    assert "line 9" not in outcome.result_summary
    assert "line 13" not in outcome.result_summary
    assert "offset=10 limit=3" in outcome.args_summary


async def test_read_file_defaults_to_first_200_lines(workspace: Path) -> None:
    (workspace / "data.txt").write_text(
        "\n".join(f"line {index}" for index in range(1, 301)),
        encoding="utf-8",
    )

    outcome = await ReadFileTool().run(
        args={"path": "data.txt"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert "[lines 1-200 of 300]" in outcome.result_summary
    assert "line 200" in outcome.result_summary
    assert "line 201" not in outcome.result_summary
    assert "offset=1 limit=200" in outcome.args_summary


async def test_read_file_rejects_out_of_range_offset(workspace: Path) -> None:
    (workspace / "data.txt").write_text("one\ntwo\n", encoding="utf-8")

    outcome = await ReadFileTool().run(
        args={"path": "data.txt", "offset": 10, "limit": 3},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is False
    assert outcome.result_summary == "offset_out_of_range"


async def test_read_file_empty_file_has_stable_range(workspace: Path) -> None:
    (workspace / "empty.txt").write_text("", encoding="utf-8")

    outcome = await ReadFileTool().run(
        args={"path": "empty.txt"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert outcome.result_summary == "[lines 0-0 of 0]\n"


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


async def test_patch_file_normalizes_whitespace_for_unique_line_window(
    workspace: Path,
) -> None:
    (workspace / "code.py").write_text(
        "def run():\n    value = 1\n    return value\n",
        encoding="utf-8",
    )
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "def run():\n  value = 1\n  return value",
            "new_text": "def run():\n  value = 2\n  return value",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert outcome.result_summary == "replaced normalized lines 1-3"
    assert (workspace / "code.py").read_text(encoding="utf-8") == (
        "def run():\n    value = 2\n    return value\n"
    )


async def test_patch_file_line_anchor_selects_exact_occurrence(workspace: Path) -> None:
    (workspace / "code.py").write_text(
        "first\nreturn value\nsecond\nreturn value\n", encoding="utf-8"
    )
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "return value",
            "new_text": "return changed",
            "start_line": 4,
            "end_line": 4,
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert (workspace / "code.py").read_text(encoding="utf-8") == (
        "first\nreturn value\nsecond\nreturn changed\n"
    )


async def test_patch_file_normalized_single_line_preserves_source_indent(
    workspace: Path,
) -> None:
    (workspace / "code.py").write_text(
        "def run():\n    return   value\n", encoding="utf-8"
    )
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "return value",
            "new_text": "return changed",
            "start_line": 2,
            "end_line": 2,
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert (workspace / "code.py").read_text(encoding="utf-8") == (
        "def run():\n    return changed\n"
    )


async def test_patch_file_rejects_unanchored_normalized_single_line(
    workspace: Path,
) -> None:
    (workspace / "code.py").write_text(
        "def run():\n    return   value\n", encoding="utf-8"
    )
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "return value",
            "new_text": "return changed",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is False
    assert outcome.result_summary == "unsafe_normalized_single_line"
    assert "requires a line anchor" in outcome.summary
    assert (workspace / "code.py").read_text(encoding="utf-8") == (
        "def run():\n    return   value\n"
    )


async def test_patch_file_preserves_cross_dedent_normalized_reindent(
    workspace: Path,
) -> None:
    original = "def run():\n    if ready:\n        work()\nreturn done\n"
    (workspace / "code.py").write_text(original, encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "if ready:\n    work()\nreturn done",
            "new_text": "if ready:\n    changed()\nreturn done",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is True
    assert (workspace / "code.py").read_text(encoding="utf-8") == (
        "def run():\n    if ready:\n        changed()\nreturn done\n"
    )


async def test_patch_file_rejects_ambiguous_intentional_reindent(
    workspace: Path,
) -> None:
    original = "def run():\n    value = 1\n    return value\n"
    (workspace / "code.py").write_text(original, encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "value = 1\nreturn value",
            "new_text": "    value = 2\n    return value",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )

    assert outcome.success is False
    assert outcome.result_summary == "unsafe_normalized_indentation"
    assert (workspace / "code.py").read_text(encoding="utf-8") == original


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


async def test_patch_file_no_match_includes_closest_matches_diagnostic(
    workspace: Path,
) -> None:
    """When old_text does not appear, surface the most similar lines and
    their line numbers so the inner-loop follow-up gives the model
    something concrete to retry with (and stays under the 1500-char cap).
    """
    file_content = (
        "def hello():\n"
        "    return 1\n"
        "\n"
        "def helo_world():\n"
        "    return 2\n"
        "\n"
        "def goodbye():\n"
        "    return 3\n"
    )
    (workspace / "src.py").write_text(file_content, encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={
            "path": "src.py",
            "old_text": "def hello_world():",
            "new_text": "def hello_world_v2():",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "no_match"
    # Diagnostic header + body must be in summary.
    assert "old_text not found in src.py" in outcome.summary
    assert "closest_matches:" in outcome.summary
    # The near-miss "def helo_world():" should rank highly and carry its
    # line number.
    assert "helo_world" in outcome.summary
    assert "L4" in outcome.summary
    # Total diagnostic stays under the documented cap.
    assert len(outcome.summary) <= 1500


async def test_patch_file_ambiguous_includes_occurrence_lines(
    workspace: Path,
) -> None:
    """When old_text matches N times, surface each occurrence's line
    number plus 1 line of context (capped at 5) so the model can
    disambiguate by widening its old_text on retry.
    """
    file_content = (
        "alpha\n"
        "    return value\n"
        "beta\n"
        "    return value\n"
        "gamma\n"
        "    return value\n"
        "delta\n"
    )
    (workspace / "code.py").write_text(file_content, encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={
            "path": "code.py",
            "old_text": "    return value",
            "new_text": "    return new",
        },
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary.startswith("ambiguous:3")
    assert "old_text matches 3 times" in outcome.summary
    assert "occurrences" in outcome.summary
    # Each of the 3 occurrence line numbers must be reported.
    assert "L2" in outcome.summary
    assert "L4" in outcome.summary
    assert "L6" in outcome.summary
    # Context labels appear for surrounding lines.
    assert "prev=" in outcome.summary or "next=" in outcome.summary
    assert len(outcome.summary) <= 1500


async def test_patch_file_ambiguous_caps_at_five_occurrences(
    workspace: Path,
) -> None:
    """The occurrence list is capped at 5 even when the file has more."""
    lines = ["target"] * 12
    (workspace / "many.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    outcome = await PatchFileTool().run(
        args={"path": "many.txt", "old_text": "target", "new_text": "X"},
        workspace_root=workspace,
        task_id="t1",
        loop_id=1,
    )
    assert outcome.success is False
    assert outcome.result_summary == "ambiguous:12"
    # Count how many "L<digit>:" entries appear at the start of a line
    # (occurrence list lines start with two spaces + L).
    occurrence_lines = [
        line for line in outcome.summary.splitlines()
        if line.startswith("  L") and ":" in line
    ]
    assert len(occurrence_lines) == 5
    assert len(outcome.summary) <= 1500


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
