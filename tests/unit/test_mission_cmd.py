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
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
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


def _seed_listing_mission(ctx: CliContext, task_id: str = "T-list") -> Mission:
    ctx.repo.create_task(task_id, "List mission state")
    mission = Mission(
        mission_id=f"mission-{task_id}",
        task_id=task_id,
        title="Listing Mission",
        description="List features and assertions.",
        phases=[
            MissionPhase(
                phase_id="phase_2",
                title="Second",
                description="Second phase",
                feature_ids=["feat_b"],
            ),
            MissionPhase(
                phase_id="phase_1",
                title="First",
                description="First phase",
                feature_ids=["feat_a", "feat_c"],
            ),
        ],
        features=[
            MissionFeature(
                feature_id="feat_c",
                hunger_item_id="H-003",
                phase_id="phase_1",
                title="C feature",
                description="third",
                status="blocked",
            ),
            MissionFeature(
                feature_id="feat_b",
                hunger_item_id="H-002",
                phase_id="phase_2",
                title="B feature",
                description="second",
                status="done",
            ),
            MissionFeature(
                feature_id="feat_a",
                hunger_item_id="H-001",
                phase_id="phase_1",
                title="A feature",
                description="first",
                status="in_progress",
            ),
        ],
        created_at=datetime.now(timezone.utc),
    )
    ctx.repo.save_mission(mission)
    ctx.repo.save_validation_contract(
        ValidationContract(
            mission_id=mission.mission_id,
            assertions=[
                ValidationAssertion(
                    assertion_id="VAL-003",
                    phase_id="phase_2",
                    title="Third assertion",
                    description="third",
                    check_type="behavioral_assertion",
                    status="blocked",
                ),
                ValidationAssertion(
                    assertion_id="VAL-002",
                    phase_id="phase_1",
                    title="Second assertion",
                    description="second",
                    check_type="behavioral_assertion",
                    status="passed",
                    validated_at_loop=8,
                ),
                ValidationAssertion(
                    assertion_id="VAL-001",
                    phase_id="phase_1",
                    title="First assertion",
                    description="first",
                    check_type="behavioral_assertion",
                    status="pending",
                ),
            ],
        )
    )
    return mission


def _mark_task_human_paused(ctx: CliContext, task_id: str) -> None:
    task = ctx.repo.get_task(task_id)
    assert task is not None
    assert isinstance(ctx.repo, InMemoryRepository)
    ctx.repo._tasks[task_id] = task.model_copy(update={"status": "HUMAN_PAUSED"})


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


def test_features_lists_every_feature_sorted_and_filterable(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx)

    result = CliRunner().invoke(
        cli,
        ["mission", "features", "T-list"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "phase_id",
        "feature_id",
        "status",
        "hunger_item_id",
        "title",
    ]
    assert [line.split(maxsplit=4)[:4] for line in lines[1:]] == [
        ["phase_1", "feat_a", "in_progress", "H-001"],
        ["phase_1", "feat_c", "blocked", "H-003"],
        ["phase_2", "feat_b", "done", "H-002"],
    ]

    phase_result = CliRunner().invoke(
        cli,
        ["mission", "features", "T-list", "--phase", "phase_2"],
        obj=ctx,
    )

    assert phase_result.exit_code == 0, phase_result.output
    assert [line.split(maxsplit=4)[:4] for line in phase_result.output.splitlines()[1:]] == [
        ["phase_2", "feat_b", "done", "H-002"]
    ]


def test_features_json_emits_required_keys(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx)

    result = CliRunner().invoke(
        cli,
        ["mission", "features", "T-list", "--json"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [
        (row["phase_id"], row["feature_id"], row["status"], row["hunger_item_id"])
        for row in payload
    ] == [
        ("phase_1", "feat_a", "in_progress", "H-001"),
        ("phase_1", "feat_c", "blocked", "H-003"),
        ("phase_2", "feat_b", "done", "H-002"),
    ]
    assert {"feature_id", "phase_id", "status", "hunger_item_id"} <= set(payload[0])


def test_validation_lists_assertions_with_last_loop(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx)

    result = CliRunner().invoke(
        cli,
        ["mission", "validation", "T-list"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "assertion_id",
        "phase_id",
        "status",
        "last_loop",
        "title",
    ]
    assert [line.split(maxsplit=4)[:4] for line in lines[1:]] == [
        ["VAL-001", "phase_1", "pending", "-"],
        ["VAL-002", "phase_1", "passed", "8"],
        ["VAL-003", "phase_2", "blocked", "-"],
    ]


def test_validation_json_filters_by_phase(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx)

    result = CliRunner().invoke(
        cli,
        ["mission", "validation", "T-list", "--phase", "phase_1", "--json"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [
        (row["assertion_id"], row["phase_id"], row["status"], row["last_loop"])
        for row in payload
    ] == [
        ("VAL-001", "phase_1", "pending", None),
        ("VAL-002", "phase_1", "passed", 8),
    ]


def test_mission_edit_non_paused_exits_7_and_records_rejection(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-edit")
    before_features = ctx.repo.list_mission_features(mission_id="mission-T-edit")
    before_assertions = ctx.repo.list_validation_assertions(mission_id="mission-T-edit")

    result = CliRunner().invoke(cli, ["mission", "edit", "T-edit"], obj=ctx)

    assert result.exit_code == 7
    assert (
        "mission import requires HUMAN_PAUSED state; use 'hungerloop hunger freeze' first"
        in result.output
    )
    assert ctx.repo.list_mission_features(mission_id="mission-T-edit") == before_features
    assert (
        ctx.repo.list_validation_assertions(mission_id="mission-T-edit")
        == before_assertions
    )
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_IMPORT_REJECTED"
    ] == ["MISSION_IMPORT_REJECTED"]


def test_mission_import_non_paused_exits_7_and_preserves_state(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    mission_before = ctx.repo.get_mission("T-import")
    assert mission_before is not None
    before = (
        mission_before,
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(_write_mission_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code == 7
    assert (
        "mission import requires HUMAN_PAUSED state; use 'hungerloop hunger freeze' first"
        in result.output
    )
    after_mission = ctx.repo.get_mission("T-import")
    assert after_mission is not None
    assert (
        after_mission,
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    ) == before
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_REJECTED"
    ] == ["MISSION_IMPORT_REJECTED"]
    assert not [
        row
        for row in ctx.repo.list_evidence("T-import", evidence_type="human_input")
        if row.get("kind") == "mission_import"
    ]


def test_mission_import_accepts_after_hunger_freeze(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    runner = CliRunner()
    import_dir = _write_mission_dir(tmp_path)
    (import_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "assertion_id": "VAL-FREEZE",
                        "phase_id": "phase-1",
                        "title": "Freeze assertion",
                        "description": "Imported after hunger freeze",
                        "check_type": "behavioral_assertion",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    freeze = runner.invoke(cli, ["hunger", "freeze", "T-import"], obj=ctx)
    assert freeze.exit_code == 0, freeze.output

    result = runner.invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(import_dir)],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "1 features added, 1 assertions added" in result.output
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_APPLIED"
    ] == ["MISSION_IMPORT_APPLIED"]
    assert not [
        event
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_REJECTED"
    ]


def test_mission_import_last_stop_reason_without_task_status_exits_7(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason="human_paused",
            goal_status="paused",
        )
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(_write_mission_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code == 7
    assert (
        "mission import requires HUMAN_PAUSED state; use 'hungerloop hunger freeze' first"
        in result.output
    )
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_REJECTED"
    ] == ["MISSION_IMPORT_REJECTED"]


def test_mission_edit_frozen_clock_without_task_status_exits_7(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-edit")
    clock = ctx.repo.get_hunger_clock("T-edit")
    clock.frozen = True
    ctx.repo.save_hunger_clock(clock)

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": "/usr/bin/false"},
    )

    assert result.exit_code == 7
    assert (
        "mission import requires HUMAN_PAUSED state; use 'hungerloop hunger freeze' first"
        in result.output
    )
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_IMPORT_REJECTED"
    ] == ["MISSION_IMPORT_REJECTED"]


def test_mission_import_paused_updates_sqlite_only_and_prints_summary(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason="human_paused",
            goal_status="paused",
        )
    )
    _mark_task_human_paused(ctx, "T-import")
    best = tmp_path / "tasks" / "T-import" / "best" / "files"
    best.mkdir(parents=True)
    mission_markdown = best / "mission.md"
    mission_markdown.write_text("# Original mirror\n", encoding="utf-8")
    features_yaml = best / "features.yaml"
    features_yaml.write_text("features: []\n", encoding="utf-8")
    contract_yaml = best / "validation-contract.yaml"
    contract_yaml.write_text("assertions: []\n", encoding="utf-8")
    services_yaml = best / "services.yaml"
    services_yaml.write_text("services: {}\n", encoding="utf-8")
    before_artifacts = {
        path.name: path.read_text(encoding="utf-8")
        for path in [mission_markdown, features_yaml, contract_yaml, services_yaml]
    }
    mission_before = ctx.repo.get_mission("T-import")
    assert mission_before is not None
    import_dir = _write_mission_dir(tmp_path)
    (import_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "assertion_id": "VAL-IMPORT",
                        "phase_id": "phase-1",
                        "title": "Imported assertion",
                        "description": "New assertion from import",
                        "check_type": "behavioral_assertion",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(import_dir)],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "1 features added, 1 assertions added" in result.output
    mission_after = ctx.repo.get_mission("T-import")
    assert mission_after is not None
    assert len(ctx.repo.list_mission_features(mission_id=mission_after.mission_id)) == 1
    assert len(
        ctx.repo.list_validation_assertions(mission_id=mission_after.mission_id)
    ) == 1
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in [mission_markdown, features_yaml, contract_yaml, services_yaml]
    } == before_artifacts
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_APPLIED"
    ] == ["MISSION_IMPORT_APPLIED"]
    import_evidence = [
        row
        for row in ctx.repo.list_evidence("T-import", evidence_type="human_input")
        if row.get("kind") == "mission_import"
    ]
    assert len(import_evidence) == 1
    assert import_evidence[0]["success"] is True


def test_mission_import_malformed_yaml_exits_2_without_writes(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason="human_paused",
            goal_status="paused",
        )
    )
    _mark_task_human_paused(ctx, "T-import")
    bad_dir = _write_mission_dir(tmp_path)
    (bad_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "phase_id": "phase-1",
                        "title": "Missing id",
                        "description": "No assertion_id",
                        "check_type": "behavioral_assertion",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mission_before = ctx.repo.get_mission("T-import")
    assert mission_before is not None
    before = (
        mission_before,
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(bad_dir)],
        obj=ctx,
    )

    assert result.exit_code == 2
    assert "assertion_id" in result.output
    assert (
        ctx.repo.get_mission("T-import"),
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    ) == before
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_LOAD_FAILED"
    ] == ["MISSION_LOAD_FAILED"]
    assert not [
        row
        for row in ctx.repo.list_evidence("T-import", evidence_type="human_input")
        if row.get("kind") == "mission_import"
    ]


def test_mission_import_compiler_failure_rolls_back_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-import")
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason="human_paused",
            goal_status="paused",
        )
    )
    _mark_task_human_paused(ctx, "T-import")
    mission_before = ctx.repo.get_mission("T-import")
    assert mission_before is not None
    before = (
        mission_before,
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    )

    def fail_compile(self: object, task_id: str, parsed_spec: object) -> object:
        raise RuntimeError("forced import failure")

    monkeypatch.setattr(
        "hungerloop.cli.mission_cmd.RequirementCompiler.compile_mission_changes",
        fail_compile,
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(_write_mission_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code != 0
    assert "forced import failure" in result.output
    assert (
        ctx.repo.get_mission("T-import"),
        ctx.repo.list_mission_features(mission_id=mission_before.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission_before.mission_id),
        ctx.repo.get_hunger_ledger("T-import"),
    ) == before
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-import")
        if event["event_type"] == "MISSION_IMPORT_FAILED"
    ] == ["MISSION_IMPORT_FAILED"]


def test_mission_edit_no_editor_exits_6(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_listing_mission(ctx, task_id="T-edit")
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-edit",
            stop_reason="human_paused",
            goal_status="paused",
        )
    )
    _mark_task_human_paused(ctx, "T-edit")

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": "", "PATH": str(tmp_path / "empty-bin")},
    )

    assert result.exit_code == 6
    assert "No editor found; set EDITOR or install vi." in result.output
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_EDIT_NO_EDITOR"
    ] == ["MISSION_EDIT_NO_EDITOR"]
