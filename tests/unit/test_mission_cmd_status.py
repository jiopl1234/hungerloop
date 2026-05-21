"""Mission cockpit rendering tests for ``hungerloop mission status``."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository


def _context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


def _seed_legacy_task(ctx: CliContext, task_id: str) -> None:
    ctx.repo.create_task(task_id, "legacy goal")
    ctx.repo.save_hunger_ledger(task_id, HungerLedger(task_id=task_id, items=[]))


def _seed_mission_cockpit(ctx: CliContext, task_id: str = "T1") -> None:
    ctx.repo.create_task(task_id, "mission goal")
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
        mission_id=f"mission-{task_id}",
        task_id=task_id,
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


def test_status_three_phases_symbols_and_contract_counts(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_mission_cockpit(ctx)

    result = CliRunner().invoke(cli, ["mission", "status", "T1"], obj=ctx)

    assert result.exit_code == 0, result.output
    assert "Mission: mission-T1 — Cockpit Mission" in result.output
    assert "[✓] phase_1 — Done phase" in result.output
    assert "[→] phase_2 — Active phase" in result.output
    assert "[ ] phase_3 — Pending phase" in result.output
    assert "[→] feat_2" in result.output
    assert "[×] feat_3" in result.output
    assert "Validation contract:" in result.output
    assert "Pending: 1" in result.output
    assert "Passed: 1" in result.output
    assert "Failed: 1" in result.output
    assert "Blocked: 1" in result.output


def test_status_json_has_documented_top_level_keys(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_mission_cockpit(ctx)

    result = CliRunner().invoke(
        cli,
        ["mission", "status", "T1", "--json"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "mission",
        "phases",
        "features_in_active_phase",
        "validation_contract",
    }
    assert payload["mission"]["mission_id"] == "mission-T1"
    symbols = {row["id"]: row["symbol"] for row in payload["phases"]}
    assert symbols == {"phase_1": "[✓]", "phase_2": "[→]", "phase_3": "[ ]"}
    feature_symbols = {
        row["id"]: row["symbol"] for row in payload["features_in_active_phase"]
    }
    assert feature_symbols["feat_3"] == "[×]"


def test_status_fallback_to_v0_5f_is_byte_identical(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_legacy_task(ctx, "T9")
    runner = CliRunner()

    mission_status = runner.invoke(cli, ["mission", "status", "T9"], obj=ctx)
    legacy_status = runner.invoke(cli, ["status", "T9"], obj=ctx)

    assert mission_status.exit_code == 0, mission_status.output
    assert legacy_status.exit_code == 0, legacy_status.output
    assert mission_status.output.rstrip() == legacy_status.output.rstrip()
