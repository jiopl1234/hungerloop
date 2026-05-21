"""CLI tests for the v0.6 ``hungerloop mission`` group."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient


def _context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


def _write_mission_dir(tmp_path: Path) -> Path:
    mission_dir = tmp_path / "mission-spec"
    mission_dir.mkdir()
    (mission_dir / "mission.md").write_text(
        "\n".join(
            [
                "# Demo Mission",
                "",
                "## Description",
                "",
                "Build the demo mission.",
                "",
                "## Phases",
                "",
                "### phase-1 Bootstrap",
                "",
                "Features:",
                "- [pending] Feature feat-1: Build report (hunger: H-001)",
                "",
                "Assertions:",
                "- [pending] Assertion VAL-001: Report exists (behavioral_assertion)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (mission_dir / "features.yaml").write_text(
        yaml.safe_dump(
            {
                "features": [
                    {
                        "feature_id": "feat-1",
                        "hunger_item_id": "H-001",
                        "phase_id": "phase-1",
                        "title": "Build report",
                        "description": "Create report.md",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (mission_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "assertion_id": "VAL-001",
                        "phase_id": "phase-1",
                        "title": "Report exists",
                        "description": "report.md should exist",
                        "check_type": "behavioral_assertion",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return mission_dir


def _mission(task_id: str) -> Mission:
    phase = MissionPhase(
        phase_id="phase-1",
        title="Bootstrap",
        description="Bootstrap phase",
        feature_ids=["feat-1"],
    )
    feature = MissionFeature(
        feature_id="feat-1",
        hunger_item_id="H-001",
        phase_id="phase-1",
        title="Build report",
        description="Create report.md",
    )
    return Mission(
        mission_id=f"mission-{task_id}",
        task_id=task_id,
        title="Demo Mission",
        description="Build the demo mission.",
        phases=[phase],
        features=[feature],
        created_at=datetime.now(timezone.utc),
    )


def test_help_lists_exact_mission_subcommands(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["mission", "--help"], obj=_context(tmp_path))

    assert result.exit_code == 0, result.output
    output_lines = result.output.splitlines()
    command_start = output_lines.index("Commands:") + 1
    command_lines = [
        line.strip().split()[0]
        for line in output_lines[command_start:]
        if line.startswith("  ") and line.strip().split()
    ]
    assert command_lines == [
        "edit",
        "features",
        "import",
        "new",
        "run",
        "status",
        "validation",
    ]


def test_new_from_directory_creates_task_mission_ledger_contract(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["mission", "new", "T1", "--from", str(_write_mission_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "task_id=T1" in result.output
    assert ctx.repo.get_task("T1") is not None
    mission = ctx.repo.get_mission("T1")
    assert mission is not None
    assert mission.title == "Demo Mission"
    assert [feature.feature_id for feature in mission.features] == ["feat-1"]
    ledger = ctx.repo.get_hunger_ledger("T1")
    assert [item.id for item in ledger.items] == ["H-001"]
    contract = ctx.repo.get_validation_contract(mission.mission_id)
    assert contract is not None
    assert [assertion.assertion_id for assertion in contract.assertions] == [
        "VAL-001"
    ]
    events = ctx.repo.list_events("T1")
    assert [event["event_type"] for event in events].count("MISSION_NEW_CREATED") == 1


def test_new_duplicate_rejected_without_rewriting_rows(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    mission_dir = _write_mission_dir(tmp_path)
    runner = CliRunner()
    first = runner.invoke(
        cli,
        ["mission", "new", "T1", "--from", str(mission_dir)],
        obj=ctx,
    )
    assert first.exit_code == 0, first.output
    mission = ctx.repo.get_mission("T1")
    assert mission is not None
    before = (
        len(ctx.repo.list_mission_features(mission_id=mission.mission_id)),
        len(ctx.repo.list_validation_assertions(mission_id=mission.mission_id)),
    )

    second = runner.invoke(
        cli,
        ["mission", "new", "T1", "--from", str(mission_dir)],
        obj=ctx,
    )

    assert second.exit_code == 2
    assert "mission already exists for task; use 'mission import' to update" in (
        second.output
    )
    assert mission is not None
    after = (
        len(ctx.repo.list_mission_features(mission_id=mission.mission_id)),
        len(ctx.repo.list_validation_assertions(mission_id=mission.mission_id)),
    )
    assert after == before


def test_new_contract_option_overrides_directory_contract(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    mission_dir = _write_mission_dir(tmp_path)
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "assertion_id": "VAL-OVERRIDE",
                        "phase_id": "phase-1",
                        "title": "Override assertion",
                        "description": "Separate contract file wins",
                        "check_type": "behavioral_assertion",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "mission",
            "new",
            "T4",
            "--from",
            str(mission_dir),
            "--contract",
            str(contract_path),
        ],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    mission = ctx.repo.get_mission("T4")
    assert mission is not None
    contract = ctx.repo.get_validation_contract(mission.mission_id)
    assert contract is not None
    assert [assertion.assertion_id for assertion in contract.assertions] == [
        "VAL-OVERRIDE"
    ]
    events = ctx.repo.list_events("T4")
    assert [event["event_type"] for event in events].count("MISSION_NEW_CREATED") == 1


def test_new_accept_uses_legacy_path_without_mission(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    accept = json.dumps({"check_keys": ["k1"]})

    result = CliRunner().invoke(
        cli,
        ["mission", "new", "T3", "--accept", accept],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert ctx.repo.get_task("T3") is not None
    assert ctx.repo.get_mission("T3") is None
    assert len(ctx.repo.get_hunger_ledger("T3").items) >= 1


def test_new_bad_from_path_exits_2_without_creating_task(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["mission", "new", "T5", "--from", str(tmp_path / "missing")],
        obj=ctx,
    )

    assert result.exit_code == 2
    assert ctx.repo.get_task("T5") is None
    assert ctx.repo.get_mission("T5") is None
    events = ctx.repo.list_events("T5", include_global=True)
    assert any(
        event["event_type"] == "MISSION_LOAD_FAILED"
        and event["payload"]["task_id"] == "T5"
        for event in events
    )


def test_mission_run_help_exposes_required_flag_parity(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["mission", "run", "--help"],
        obj=_context(tmp_path),
    )

    assert result.exit_code == 0, result.output
    for flag in (
        "--max-loops",
        "--refill",
        "--resume",
        "--reset",
        "--refinement-profile",
        "--spend-budget",
        "--skip-repair-check",
    ):
        assert flag in result.output


def test_mission_runtime_zero_prints_notice_and_uses_legacy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path)
    task_id = "T-runtime"
    ctx.repo.create_task(task_id, "Build report")
    ctx.repo.save_mission(_mission(task_id))
    item = HungerItem(
        id="H-001",
        title="report",
        status=HungerItemStatus.OPEN,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
            )
        ],
    )
    ctx.repo.save_hunger_ledger(task_id, HungerLedger(task_id=task_id, items=[item]))
    ctx.model_client = DummyModelClient.with_actions(
        [{"tool_name": "write_file", "args": {"path": "report.md", "content": "ok"}}]
    )
    monkeypatch.setenv("HUNGERLOOP_MISSION_RUNTIME", "0")

    result = CliRunner().invoke(
        cli,
        ["mission", "run", task_id, "--max-loops", "2"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "HUNGERLOOP_MISSION_RUNTIME" in result.output
    assert not [
        event
        for event in ctx.repo.list_events(task_id)
        if str(event["event_type"]).startswith("mission.")
    ]
