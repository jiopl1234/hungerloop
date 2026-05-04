"""``hungerloop report`` command tests (PRD §18.3).

Pins the v1 JSON schema, the markdown template's distinctness from
``hungerloop status``, and the empty/running/done/error states.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.cli.report_format import REPORT_SCHEMA_VERSION, build_report_dict
from hungerloop.models.enums import (
    HungerItemStatus,
    LoopPhase,
    StopReason,
)
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "demo-1",
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.save_hunger_ledger(
        "demo-1", HungerLedger(task_id="demo-1", items=[])
    )
    return CliContext(repo=repo, workspace_root=tmp_path)


def _seed_done_task(ctx: CliContext) -> None:
    """Common fixture: a finished task with two committed loops + accepted
    checks + a completed StopReport."""
    repo = ctx.repo
    h = HungerItem(
        id="H-001",
        title="x",
        gap_score=0.0,
        status=HungerItemStatus.VALIDATED_SATISFIED,
    )
    repo.save_hunger_ledger(
        "demo-1", HungerLedger(task_id="demo-1", items=[h])
    )
    repo.save_hunger_item(h)

    repo.save_loop_trace(
        LoopTrace(
            task_id="demo-1",
            loop_id=1,
            phase=LoopPhase.EXPLORE.value,
            active_hunger=80.0,
            drive_budget=80.0,
            work_pressure=10.0,
            committed=True,
            delta_summary="committed (newly_passed: H-001:0)",
            next_action="continue",
        )
    )
    repo.save_loop_trace(
        LoopTrace(
            task_id="demo-1",
            loop_id=2,
            phase=LoopPhase.EXPLOIT.value,
            active_hunger=60.0,
            drive_budget=60.0,
            work_pressure=0.0,
            committed=False,
            delta_summary="rejected (no progress)",
            next_action="continue",
        )
    )
    from hungerloop.models.blackboard import BestState

    repo.save_best_state(
        BestState(
            task_id="demo-1",
            state_id="STATE-demo-1-1",
            summary="best",
            accepted_check_keys=["H-001:0"],
        )
    )
    repo.save_stop_report(
        StopReport(
            task_id="demo-1",
            stop_reason=StopReason.DONE,
            goal_status="completed",
            recommendation="",
            total_loops=2,
        )
    )


# ---------------------------------------------------------------------------
# JSON schema
# ---------------------------------------------------------------------------


def test_report_emits_schema_version_1(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION


def test_report_running_task_has_null_stop_reason(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    payload = json.loads(result.output)
    assert payload["stop"]["reason"] is None
    assert payload["stop"]["goal_status"] == "in_progress"
    assert payload["last_loop"] is None


def test_report_done_task_includes_accepted_check_keys(
    context: CliContext,
) -> None:
    _seed_done_task(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stop"]["reason"] == "done"
    assert payload["stop"]["goal_status"] == "completed"
    assert payload["accepted_check_keys"] == ["H-001:0"]
    assert payload["best_state_id"] == "STATE-demo-1-1"
    assert payload["loops"] == {"total": 2, "committed": 1, "rejected": 1}
    assert payload["last_loop"]["loop_id"] == 2  # chronologically last


def test_report_unknown_task_exits_1_with_stderr(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "missing-1"], obj=context)
    assert result.exit_code == 1
    # Click's CliRunner mixes stderr into output by default; with the
    # err=True echo we still see the message in result.output.
    assert "Task not found: missing-1" in result.output


def test_report_json_round_trips_through_json_loads(
    context: CliContext,
) -> None:
    _seed_done_task(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    # Should parse without error; this is the wire-contract assertion.
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    assert all(
        key in payload
        for key in (
            "schema_version",
            "task_id",
            "goal",
            "stop",
            "ledger",
            "usage",
            "loops",
            "best_state_id",
            "accepted_check_keys",
            "last_loop",
        )
    )


def test_report_all_keys_always_present_even_on_running_task(
    context: CliContext,
) -> None:
    """Schema rule: keys are always present; absence is null/[]."""
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    payload = json.loads(result.output)
    assert payload["best_state_id"] is None
    assert payload["accepted_check_keys"] == []
    assert payload["last_loop"] is None
    assert payload["goal"] is None  # TaskRecord lands with SQLiteRepo


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_report_markdown_contains_headline_and_usage(
    context: CliContext,
) -> None:
    _seed_done_task(context)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["report", "demo-1", "--format", "markdown"], obj=context
    )
    assert result.exit_code == 0, result.output
    out = result.output
    # Headline mentions the task id and the stop reason.
    assert "# Task `demo-1`" in out
    assert "done" in out
    # Usage section present.
    assert "## Usage" in out


def test_report_markdown_does_not_match_status_byteforbyte(
    context: CliContext,
) -> None:
    """Decision §11.1: ``report --format markdown`` and ``status`` are
    deliberately distinct templates so they can evolve independently."""
    _seed_done_task(context)
    runner = CliRunner()

    md = runner.invoke(
        cli, ["report", "demo-1", "--format", "markdown"], obj=context
    ).output
    status = runner.invoke(cli, ["status", "demo-1"], obj=context).output

    assert md != status
    # Plus a structural cue: the markdown report uses headers; status
    # uses key:value lines.
    assert md.startswith("# Task")
    assert status.startswith("task_id:")


def test_report_markdown_running_task_says_running(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["report", "demo-1", "--format", "markdown"], obj=context
    )
    assert "running" in result.output.lower()


# ---------------------------------------------------------------------------
# build_report_dict directly (no Click)
# ---------------------------------------------------------------------------


def test_build_report_dict_done_task_shape(context: CliContext) -> None:
    _seed_done_task(context)
    payload = build_report_dict(context.repo, "demo-1")
    assert payload["ledger"] == {
        "items_total": 1,
        "items_satisfied": 1,
        "items_blocked": 0,
        "items_open": 0,
    }
    # Cost is rounded to 6 decimal places to keep the JSON stable.
    assert isinstance(payload["usage"]["cost_usd"], float)


def test_build_report_dict_loops_committed_vs_rejected(
    context: CliContext,
) -> None:
    """Counts come from LoopTrace.committed; verify both flavors."""
    _seed_done_task(context)
    payload = build_report_dict(context.repo, "demo-1")
    assert payload["loops"]["committed"] == 1
    assert payload["loops"]["rejected"] == 1
