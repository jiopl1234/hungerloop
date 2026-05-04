"""Click CliRunner tests for `workspace` and `checks` (v0.4.1 inspectors).

`coverage` reported these at 44% / 31% before this file landed — every
branch (missing dir, dir with files, db missing, table missing, populated
table) was untested. They were carried forward from v0.4.1 without the
test net the v0.5a commands got.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.checks_cmd import checks
from hungerloop.cli.workspace_cmd import workspace

# ---- workspace best ---------------------------------------------------------


def test_workspace_best_missing_dir_emits_friendly_message(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(workspace, ["best", "t1", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No best workspace for task t1" in result.output


def test_workspace_best_lists_files_with_sizes(tmp_path: Path) -> None:
    best_dir = tmp_path / "tasks" / "t1" / "best" / "files"
    best_dir.mkdir(parents=True)
    (best_dir / "a.md").write_text("hello")
    (best_dir / "nested").mkdir()
    (best_dir / "nested" / "b.txt").write_text("xy")

    runner = CliRunner()
    result = runner.invoke(workspace, ["best", "t1", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "a.md" in result.output
    assert "5 bytes" in result.output
    # rglob recurses; nested file shown by relative path.
    assert "nested/b.txt" in result.output
    assert "2 bytes" in result.output


# ---- workspace candidate ----------------------------------------------------


def test_workspace_candidate_missing_dir_emits_friendly_message(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        workspace,
        ["candidate", "t1", "--loop", "1", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "No candidate workspace for task t1 loop 1" in result.output


def test_workspace_candidate_lists_files_with_zero_padded_loop(
    tmp_path: Path,
) -> None:
    cand_dir = (
        tmp_path / "tasks" / "t1" / "candidates" / "loop_007" / "files"
    )
    cand_dir.mkdir(parents=True)
    (cand_dir / "report.md").write_text("ok")

    runner = CliRunner()
    result = runner.invoke(
        workspace,
        ["candidate", "t1", "--loop", "7", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "report.md" in result.output


# ---- workspace rejected -----------------------------------------------------


def test_workspace_rejected_missing_dir_emits_friendly_message(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        workspace,
        ["rejected", "t1", "--loop", "1", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "No rejected workspace for task t1 loop 1" in result.output


def test_workspace_rejected_lists_files(tmp_path: Path) -> None:
    rej_dir = tmp_path / "tasks" / "t1" / "rejected" / "loop_001" / "files"
    rej_dir.mkdir(parents=True)
    (rej_dir / "broken.md").write_text("oops")

    runner = CliRunner()
    result = runner.invoke(
        workspace,
        ["rejected", "t1", "--loop", "1", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "broken.md" in result.output


# ---- checks -----------------------------------------------------------------


def test_checks_no_db_emits_friendly_message(tmp_path: Path) -> None:
    runner = CliRunner()
    db = tmp_path / "missing.sqlite"
    result = runner.invoke(checks, ["t1", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert f"No database found at {db}" in result.output


def test_checks_db_without_table_emits_friendly_message(tmp_path: Path) -> None:
    """An empty SQLite file has no `accepted_checks` table — the command
    must surface that as a polite message, not a stack trace."""
    db = tmp_path / "blackboard.sqlite"
    sqlite3.connect(str(db)).close()  # creates an empty db

    runner = CliRunner()
    result = runner.invoke(checks, ["t1", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "accepted_checks table not found" in result.output


def test_checks_empty_table_emits_no_accepted_checks(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE accepted_checks ("
        "task_id TEXT, check_key TEXT, hunger_item_id TEXT, "
        "check_index INTEGER, accepted_at_loop INTEGER, validation_id TEXT)"
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(checks, ["t1", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "No accepted checks." in result.output


def test_checks_populated_table_lists_rows(tmp_path: Path) -> None:
    db = tmp_path / "blackboard.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE accepted_checks ("
        "task_id TEXT, check_key TEXT, hunger_item_id TEXT, "
        "check_index INTEGER, accepted_at_loop INTEGER, validation_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO accepted_checks VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("t1", "H-001:0", "H-001", 0, 3, "VAL-t1-3"),
            ("t1", "H-002:0", "H-002", 0, 5, "VAL-t1-5"),
            ("other-task", "H-X:0", "H-X", 0, 1, "VAL-other-1"),
        ],
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(checks, ["t1", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "H-001:0" in result.output
    assert "H-002:0" in result.output
    assert "loop=3" in result.output
    assert "VAL-t1-5" in result.output
    # Other-task rows must be filtered out.
    assert "other-task" not in result.output
    assert "H-X:0" not in result.output
