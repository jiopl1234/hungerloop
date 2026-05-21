"""``hungerloop report`` command tests (PRD §18.3).

Pins the v1 JSON schema, the markdown template's distinctness from
``hungerloop status``, and the empty/running/done/error states.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
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


def _seed_mission_cockpit(ctx: CliContext) -> None:
    """Seed a mission row that should augment ``hungerloop report``."""
    phases = [
        MissionPhase(
            phase_id="phase_1",
            title="Done phase",
            description="Done",
            feature_ids=["feat_1"],
            validation_contract_ids=["VAL-PASSED"],
            status="done",
        ),
        MissionPhase(
            phase_id="phase_2",
            title="Active phase",
            description="Active",
            feature_ids=["feat_2", "feat_3"],
            validation_contract_ids=["VAL-PENDING", "VAL-FAILED"],
            status="validating",
        ),
        MissionPhase(
            phase_id="phase_3",
            title="Pending phase",
            description="Pending",
            status="pending",
        ),
    ]
    features = [
        MissionFeature(
            feature_id="feat_1",
            hunger_item_id="H-001",
            phase_id="phase_1",
            title="Finished feature",
            description="done",
            status="done",
            assigned_worker_ids=["worker_a"],
        ),
        MissionFeature(
            feature_id="feat_2",
            hunger_item_id="H-002",
            phase_id="phase_2",
            title="Running feature",
            description="running",
            status="in_progress",
            assigned_worker_ids=["worker_b"],
        ),
        MissionFeature(
            feature_id="feat_3",
            hunger_item_id="H-003",
            phase_id="phase_2",
            title="Blocked feature",
            description="blocked",
            status="blocked",
        ),
    ]
    mission = Mission(
        mission_id="mission-demo-1",
        task_id="demo-1",
        title="Cockpit Mission",
        description="Show mission state.",
        phases=phases,
        features=features,
        created_at=datetime.now(timezone.utc),
    )
    ctx.repo.save_mission(mission)
    ctx.repo.save_validation_contract(
        ValidationContract(
            mission_id=mission.mission_id,
            assertions=[
                ValidationAssertion(
                    assertion_id="VAL-PASSED",
                    phase_id="phase_1",
                    title="Passed",
                    description="passed",
                    check_type="behavioral_assertion",
                    status="passed",
                    validated_at_loop=12,
                ),
                ValidationAssertion(
                    assertion_id="VAL-PENDING",
                    phase_id="phase_2",
                    title="Pending",
                    description="pending",
                    check_type="behavioral_assertion",
                    status="pending",
                ),
                ValidationAssertion(
                    assertion_id="VAL-FAILED",
                    phase_id="phase_2",
                    title="Failed",
                    description="failed",
                    check_type="behavioral_assertion",
                    status="failed",
                    validated_at_loop=15,
                ),
                ValidationAssertion(
                    assertion_id="VAL-BLOCKED",
                    phase_id="phase_3",
                    title="Blocked assertion",
                    description="blocked",
                    check_type="behavioral_assertion",
                    status="blocked",
                ),
            ],
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


def test_report_without_mission_preserves_v0_5f_json_shape(
    context: CliContext,
) -> None:
    result = CliRunner().invoke(cli, ["report", "demo-1"], obj=context)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "mission_cockpit" not in payload
    assert "mission_cockpit_text" not in payload
    assert "Mission:" not in result.output


def test_report_with_mission_includes_shared_cockpit_json(
    context: CliContext,
) -> None:
    _seed_mission_cockpit(context)
    runner = CliRunner()

    report_result = runner.invoke(cli, ["report", "demo-1"], obj=context)
    status_result = runner.invoke(cli, ["mission", "status", "demo-1"], obj=context)

    assert report_result.exit_code == 0, report_result.output
    assert status_result.exit_code == 0, status_result.output
    payload = json.loads(report_result.output)
    assert payload["mission_cockpit_text"] == status_result.output.rstrip()
    assert payload["mission_cockpit"]["mission"]["mission_id"] == "mission-demo-1"
    assert [row["symbol"] for row in payload["mission_cockpit"]["phases"]] == [
        "[✓]",
        "[→]",
        "[ ]",
    ]
    assert "Mission: mission-demo-1 — Cockpit Mission" in report_result.output


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


def test_report_markdown_with_mission_appends_shared_cockpit(
    context: CliContext,
) -> None:
    _seed_mission_cockpit(context)
    runner = CliRunner()

    report_result = runner.invoke(
        cli, ["report", "demo-1", "--format", "markdown"], obj=context
    )
    status_result = runner.invoke(cli, ["mission", "status", "demo-1"], obj=context)

    assert report_result.exit_code == 0, report_result.output
    assert status_result.exit_code == 0, status_result.output
    assert status_result.output.rstrip() in report_result.output
    assert "## Mission cockpit" in report_result.output


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
