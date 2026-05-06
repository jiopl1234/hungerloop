"""Click CliRunner tests for v0.5a CLI commands (PRD §18)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    StopReason,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.tracing import StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.openai_model_client import OpenAIModelClient


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.get_hunger_clock("t1")
    return CliContext(repo=repo, workspace_root=tmp_path)


def test_default_main_without_context_raises_clear_error() -> None:
    """Production entry point opens a SQLite-backed context by default."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["status", "t1"])
    assert result.exit_code == 0
    assert "task_id: t1" in result.output


# ---- new ----


def test_new_creates_task_with_acceptance_check(context: CliContext) -> None:
    runner = CliRunner()
    accept = json.dumps(
        {
            "check_type": "file_exists",
            "params": {"path": "report.md"},
            "description": "Report file exists",
        }
    )
    result = runner.invoke(
        cli,
        [
            "new",
            "Build a report",
            "--accept",
            accept,
            "--task-id",
            "demo-1",
        ],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    assert "Created task: demo-1" in result.output
    ledger = context.repo.get_hunger_ledger("demo-1")
    assert ledger.items[0].id == "H-001"
    assert (
        ledger.items[0].acceptance_checks[0].check_type
        is AcceptanceCheckType.FILE_EXISTS
    )


def test_new_rejects_invalid_json(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["new", "Goal", "--accept", "not-json", "--task-id", "demo-2"],
        obj=context,
    )
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_new_accept_file_yaml(context: CliContext, tmp_path: Path) -> None:
    accept_file = tmp_path / "accept.yaml"
    accept_file.write_text(
        """
core_acceptance_mode: any
core_acceptance_checks:
  - check_type: file_exists
    params:
      path: report.md
    description: report exists
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "new",
            "Build from file",
            "--accept-file",
            str(accept_file),
            "--task-id",
            "demo-file",
        ],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    ledger = context.repo.get_hunger_ledger("demo-file")
    assert ledger.items[0].acceptance_mode == "any"
    assert ledger.items[0].acceptance_checks[0].params == {"path": "report.md"}
    assert context.repo.get_task("demo-file").raw_goal == "Build from file"  # type: ignore[union-attr]


def test_new_requires_accept_or_accept_file(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["new", "Goal"], obj=context)
    assert result.exit_code != 0
    assert "--accept JSON spec or --accept-file" in result.output


# ---- status ----


def test_status_prints_task_summary(context: CliContext) -> None:
    runner = CliRunner()
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    result = runner.invoke(cli, ["status", "t1"], obj=context)
    assert result.exit_code == 0, result.output
    assert "task_id: t1" in result.output


# ---- hunger group ----


def test_hunger_refill_decrements_loop_count(context: CliContext) -> None:
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    clock = context.repo.get_hunger_clock("t1")
    clock.loop_count = 8
    context.repo.save_hunger_clock(clock)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "refill", "t1", "--loops", "5"], obj=context
    )
    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_clock("t1").loop_count == 3


def test_hunger_refill_rejects_zero(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "refill", "t1", "--loops", "0"], obj=context
    )
    assert result.exit_code != 0


def test_hunger_unblock_flips_single_item(context: CliContext) -> None:
    item = HungerItem(
        id="H-001",
        title="x",
        status=HungerItemStatus.BLOCKED,
        consecutive_failure_count=3,
    )
    context.repo.save_hunger_ledger(
        "t1", HungerLedger(task_id="t1", items=[item])
    )
    context.repo.save_hunger_item(item)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "unblock", "t1", "H-001"], obj=context
    )
    assert result.exit_code == 0, result.output
    fetched = context.repo.get_hunger_item("H-001")
    assert fetched is not None
    assert fetched.status is HungerItemStatus.OPEN
    assert fetched.consecutive_failure_count == 0


def test_hunger_unblock_unknown_item_errors(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "unblock", "t1", "H-missing"], obj=context
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_hunger_unblock_all_flips_every_blocked(context: CliContext) -> None:
    items = [
        HungerItem(id="H-001", title="a", status=HungerItemStatus.BLOCKED),
        HungerItem(id="H-002", title="b", status=HungerItemStatus.BLOCKED),
        HungerItem(id="H-003", title="c", status=HungerItemStatus.OPEN),
    ]
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=items))
    for item in items:
        context.repo.save_hunger_item(item)

    runner = CliRunner()
    result = runner.invoke(cli, ["hunger", "unblock-all", "t1"], obj=context)
    assert result.exit_code == 0, result.output
    statuses = {
        i.id: context.repo.get_hunger_item(i.id).status  # type: ignore[union-attr]
        for i in items
    }
    assert statuses["H-001"] is HungerItemStatus.OPEN
    assert statuses["H-002"] is HungerItemStatus.OPEN
    assert statuses["H-003"] is HungerItemStatus.OPEN


def test_hunger_freeze_then_resume(context: CliContext) -> None:
    runner = CliRunner()
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))

    result = runner.invoke(cli, ["hunger", "freeze", "t1"], obj=context)
    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_clock("t1").frozen is True

    result = runner.invoke(cli, ["hunger", "resume", "t1"], obj=context)
    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_clock("t1").frozen is False


# ---- run (orchestrator integration) ----


def test_run_completes_a_simple_task(context: CliContext) -> None:
    """End-to-end: new task with file_exists check + scripted dummy ->
    DONE on first or second loop."""
    item = HungerItem(
        id="H-001",
        title="report",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
            )
        ],
    )
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    context.repo.save_hunger_item(item)
    context.model_client = DummyModelClient.with_actions(
        [
            {
                "tool_name": "write_file",
                "args": {"path": "report.md", "content": "ok"},
            }
        ]
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=context)
    assert result.exit_code == 0, result.output
    assert "stopped: done" in result.output
    assert "goal_status: completed" in result.output
    # Per §28.16 / M4 the CLI persists the StopReport, not the orchestrator.
    assert context.repo.get_last_stop_reason("t1") is StopReason.DONE


def test_run_preflight_blocks_on_hunger_expired_without_refill(
    context: CliContext,
) -> None:
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    context.repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUNGER_EXPIRED,
            goal_status="abandoned",
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=context)
    assert result.exit_code == 2
    assert "HUNGER_EXPIRED" in result.output


def test_run_with_refill_passes_preflight(context: CliContext) -> None:
    item = HungerItem(
        id="H-001",
        title="x",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
            )
        ],
    )
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    context.repo.save_hunger_item(item)
    context.repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUNGER_EXPIRED,
            goal_status="abandoned",
        )
    )
    clock = context.repo.get_hunger_clock("t1")
    clock.loop_count = 5
    context.repo.save_hunger_clock(clock)

    context.model_client = DummyModelClient.with_actions(
        [
            {
                "tool_name": "write_file",
                "args": {"path": "report.md", "content": "ok"},
            }
        ]
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1", "--refill", "5"], obj=context)
    assert result.exit_code == 0, result.output
    # loop_count was 5 before --refill 5 then incremented per loop run.
    # We just verify the CLI didn't error on preflight.


def test_run_raise_cost_ceiling_updates_policy(context: CliContext) -> None:
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    context.repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.SAFETY_STOP,
            goal_status="abandoned",
        )
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "t1", "--raise-cost-ceiling", "100.0"],
        obj=context,
    )
    # Even though ledger is empty (HungerEngine -> DONE quickly), the CLI
    # must apply the ceiling raise *before* preflight.
    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_policy("t1").max_total_cost_usd == 100.0


def test_run_resume_unfreezes_clock_and_emits_event(
    context: CliContext,
) -> None:
    """`run --resume` on a HUMAN_PAUSED task must flip clock.frozen
    back to False and append a hunger_resumed event so the orchestrator
    sees an unblocked clock on the next tick."""
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    context.repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )
    clock = context.repo.get_hunger_clock("t1")
    clock.frozen = True
    context.repo.save_hunger_clock(clock)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "t1", "--resume", "--max-loops", "1"], obj=context
    )
    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_clock("t1").frozen is False
    events = [
        e for e in context.repo._events if e["event_type"] == "hunger_resumed"
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {"via": "run --resume"}


def test_run_resume_on_unfrozen_clock_is_idempotent(
    context: CliContext,
) -> None:
    """`--resume` on an already-unfrozen clock must not emit a
    redundant hunger_resumed event (the flag is no-op when there's
    nothing to resume)."""
    context.repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    context.repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )
    # Clock starts unfrozen by default.

    runner = CliRunner()
    runner.invoke(cli, ["run", "t1", "--resume", "--max-loops", "1"], obj=context)
    events = [
        e for e in context.repo._events if e["event_type"] == "hunger_resumed"
    ]
    assert events == []


def test_run_model_config_dummy_resolves_client(
    context: CliContext, tmp_path: Path
) -> None:
    from hungerloop.cli.run_cmd import _resolve_model_client

    path = tmp_path / "model.yaml"
    path.write_text("provider: dummy\nmodel_name: dummy\n", encoding="utf-8")
    client = _resolve_model_client(context, path)
    assert isinstance(client, DummyModelClient)


def test_run_model_config_openai_resolves_client(
    context: CliContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hungerloop.cli.run_cmd import _resolve_model_client

    monkeypatch.setenv("HL_TEST_API_KEY", "sk-test")
    path = tmp_path / "model.yaml"
    path.write_text(
        (
            "provider: openai\n"
            "model_name: gpt-4o-mini\n"
            "api_key_env: HL_TEST_API_KEY\n"
        ),
        encoding="utf-8",
    )
    client = _resolve_model_client(context, path)
    assert isinstance(client, OpenAIModelClient)


def test_run_model_config_unknown_openai_pricing_requires_confirmation(
    context: CliContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hungerloop.cli.run_cmd import _resolve_model_client

    monkeypatch.setenv("HL_TEST_API_KEY", "sk-test")
    path = tmp_path / "model.yaml"
    path.write_text(
        (
            "provider: openai\n"
            "model_name: glm-5.1\n"
            "api_key_env: HL_TEST_API_KEY\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no configured pricing"):
        _resolve_model_client(context, path)


def test_run_model_config_unknown_openai_pricing_can_be_accepted(
    context: CliContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hungerloop.cli.run_cmd import _resolve_model_client

    monkeypatch.setenv("HL_TEST_API_KEY", "sk-test")
    path = tmp_path / "model.yaml"
    path.write_text(
        (
            "provider: openai\n"
            "model_name: glm-5.1\n"
            "api_key_env: HL_TEST_API_KEY\n"
        ),
        encoding="utf-8",
    )
    client = _resolve_model_client(
        context, path, accept_unknown_pricing=True
    )
    assert isinstance(client, OpenAIModelClient)


def test_run_model_config_azure_fails_clearly(
    context: CliContext, tmp_path: Path
) -> None:
    path = tmp_path / "model.yaml"
    path.write_text("provider: azure_openai\nmodel_name: gpt-4o\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "t1", "--model-config", str(path), "--max-loops", "1"],
        obj=context,
    )
    assert result.exit_code != 0
    assert "Azure runtime" in result.output
