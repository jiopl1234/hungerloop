"""``hungerloop trace export`` JSONL contract tests (PRD §22.8).

Pins the wire shape: one JSON object per line, the field set
``{task_id, loop_id, event_type, payload, created_at}``, and the exit
codes (0 for empty/clean, 1 for unknown task).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerLedger, HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "demo-1",
        HungerPolicy(
            max_total_cost_usd=1.0,
            max_total_tokens=10_000,
            initial_hunger=10.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.save_hunger_ledger(
        "demo-1", HungerLedger(task_id="demo-1", items=[])
    )
    return CliContext(repo=repo, workspace_root=tmp_path)


def _seed_events(ctx: CliContext) -> None:
    """Push 3 events for demo-1 across two loops + one for a sibling task."""
    ctx.repo.append_event(
        EventType.LOOP_STARTED,
        {"loop_id": 1},
        task_id="demo-1",
        loop_id=1,
    )
    ctx.repo.append_event(
        EventType.LOOP_COMMITTED,
        {"newly_passed": ["H-001:0"]},
        task_id="demo-1",
        loop_id=1,
    )
    ctx.repo.append_event(
        EventType.LOOP_REJECTED,
        {"reason": "no progress"},
        task_id="demo-1",
        loop_id=2,
    )
    # Sibling task — must NOT appear in demo-1's export.
    ctx.repo.append_event(
        EventType.LOOP_STARTED,
        {},
        task_id="other-task",
        loop_id=1,
    )


# ---------------------------------------------------------------------------
# JSONL shape
# ---------------------------------------------------------------------------


def test_export_emits_one_line_per_event(context: CliContext) -> None:
    _seed_events(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.split("\n") if line]
    assert len(lines) == 3


def test_export_chronological_order(context: CliContext) -> None:
    _seed_events(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    lines = [line for line in result.output.split("\n") if line]
    parsed = [json.loads(line) for line in lines]
    assert [r["event_type"] for r in parsed] == [
        "loop_started",
        "loop_committed",
        "loop_rejected",
    ]
    # loop_id ordering follows the append order; chronological for the
    # in-memory repo is identical to insertion order.
    assert [r["loop_id"] for r in parsed] == [1, 1, 2]


def test_export_each_line_is_valid_json(context: CliContext) -> None:
    _seed_events(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    for line in [line for line in result.output.split("\n") if line]:
        # Must round-trip through json.loads without error.
        payload = json.loads(line)
        assert set(payload.keys()) == {
            "task_id",
            "loop_id",
            "event_type",
            "payload",
            "created_at",
        }


def test_export_event_type_field_uses_enum_value(context: CliContext) -> None:
    """Pin the b0-05 contract: stored ``event_type`` is the enum's
    ``.value`` (snake_case string), not the enum repr."""
    _seed_events(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    parsed = [
        json.loads(line)
        for line in result.output.split("\n")
        if line
    ]
    types = {row["event_type"] for row in parsed}
    # Every emitted type is a member of the EventType enum.
    valid = {member.value for member in EventType}
    assert types <= valid


def test_export_filters_to_requested_task(context: CliContext) -> None:
    """Sibling tasks' events must not leak into the export."""
    _seed_events(context)
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    parsed = [
        json.loads(line) for line in result.output.split("\n") if line
    ]
    assert all(row["task_id"] == "demo-1" for row in parsed)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_export_empty_task_emits_no_lines_exits_zero(
    context: CliContext,
) -> None:
    """Task exists but has no events → empty stdout, exit 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    assert result.exit_code == 0
    # No JSON content; only the trailing newline Click might add.
    assert result.output.strip() == ""


def test_export_unknown_task_exits_1(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "missing-1"], obj=context)
    assert result.exit_code == 1
    assert "Task not found" in result.output


def test_export_default_format_is_jsonl(context: CliContext) -> None:
    """``--format`` is a single-choice today; verify the default lines up
    with the documented wire contract."""
    runner = CliRunner()
    result_default = runner.invoke(
        cli, ["trace", "export", "demo-1"], obj=context
    )
    result_explicit = runner.invoke(
        cli,
        ["trace", "export", "demo-1", "--format", "jsonl"],
        obj=context,
    )
    assert result_default.output == result_explicit.output


def test_export_excludes_global_events(context: CliContext) -> None:
    """Global events (``task_id IS NULL``) are NOT included in a
    per-task export — surfacing them in every task's stream would
    double-count cross-task signals like ``unknown_model_pricing``.
    """
    context.repo.append_event(
        EventType.UNKNOWN_MODEL_PRICING,
        {"model": "ghost"},
        task_id=None,
        loop_id=None,
    )
    context.repo.append_event(
        EventType.LOOP_STARTED,
        {},
        task_id="demo-1",
        loop_id=1,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    parsed = [
        json.loads(line) for line in result.output.split("\n") if line
    ]
    types = {r.get("event_type") for r in parsed}
    assert types == {"loop_started"}
    assert "unknown_model_pricing" not in types


def test_export_includes_v0_6_handoff_events_without_allow_list(
    context: CliContext,
) -> None:
    """Trace export streams all task events generically, including M2 handoff
    event types, without a per-event allow-list.
    """
    expected = [
        EventType.WORKER_HANDOFF_EMITTED,
        EventType.WORKER_HANDOFF_RECEIVED,
        EventType.WORKER_HANDOFF_BLOCKER_RECORDED,
        EventType.HANDOFF_BLOCKER_ON_CLOSED_ITEM,
    ]
    for event_type in expected:
        context.repo.append_event(
            event_type,
            {
                "mission_id": "mission-1",
                "phase_id": "phase-1",
                "feature_id": "feature-1",
                "assignment_id": "assignment-1",
            },
            task_id="demo-1",
            loop_id=1,
        )
    runner = CliRunner()

    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)

    assert result.exit_code == 0, result.output
    parsed = [
        json.loads(line) for line in result.output.split("\n") if line
    ]
    event_types = {row["event_type"] for row in parsed}
    assert {event_type.value for event_type in expected} <= event_types


def test_export_includes_all_v0_6_mission_runtime_events_without_allow_list(
    context: CliContext,
) -> None:
    """Trace export generically streams every v0.6 mission runtime event
    family rather than whitelisting only older loop or handoff names.
    """
    expected = _v0_6_mission_runtime_event_types()
    for event_type in expected:
        event_name = event_type.value if isinstance(event_type, EventType) else event_type
        context.repo.append_event(
            event_type,
            {
                "mission_id": "mission-1",
                "phase_id": "phase-1",
                "feature_id": "feature-1",
                "assignment_id": "assignment-1",
                "event_name": event_name,
            },
            task_id="demo-1",
            loop_id=1,
        )
    runner = CliRunner()

    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)

    assert result.exit_code == 0, result.output
    parsed = [
        json.loads(line) for line in result.output.split("\n") if line
    ]
    event_types = {row["event_type"] for row in parsed}
    assert {
        event_type.value if isinstance(event_type, EventType) else event_type
        for event_type in expected
    } <= event_types


def _v0_6_mission_runtime_event_types() -> list[EventType | str]:
    prefixes = (
        "mission.phase_",
        "worker.assignment_",
        "worker.handoff_",
        "validation.scrutiny_",
        "validation.user_testing_",
    )
    return [
        *[
            event_type
            for event_type in EventType
            if event_type.value.startswith(prefixes)
        ],
        EventType.MISSION_STATE_REGENERATED,
        # M6 requires trace export to avoid enum/allow-list coupling, so
        # include a forward-compatible event name that is not in EventType.
        "worker.handoff_processed",
    ]


def test_export_non_json_payload_handled_gracefully(
    context: CliContext,
) -> None:
    """A payload smuggling a datetime / set / custom object must not
    crash the exporter mid-stream — values get stringified instead."""
    from datetime import datetime, timezone

    context.repo.append_event(
        EventType.LOOP_STARTED,
        # ``datetime`` is the realistic case (skill-card emitter, future
        # memory-promotion logs); ``object()`` is the worst case.
        {
            "started_at": datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
            "marker": object(),
        },
        task_id="demo-1",
        loop_id=1,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["trace", "export", "demo-1"], obj=context)
    assert result.exit_code == 0
    [line] = [line for line in result.output.split("\n") if line]
    parsed = json.loads(line)
    # Date got stringified; opaque objects got their ``str()`` repr.
    assert isinstance(parsed["payload"]["started_at"], str)
    assert "2026-05-04" in parsed["payload"]["started_at"]
    assert isinstance(parsed["payload"]["marker"], str)
