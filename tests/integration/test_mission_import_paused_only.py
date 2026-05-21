"""Integration coverage for ADR-009 mission import semantics."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.enums import StopReason
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.sqlite_repo import SQLiteRepository


def _context(tmp_path: Path) -> tuple[CliContext, Path]:
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    return CliContext(repo=repo, workspace_root=tmp_path), db_path


def _seed_mission(ctx: CliContext, task_id: str = "T-import") -> None:
    ctx.repo.create_task(task_id, "Import mission")
    mission = Mission(
        mission_id=f"mission-{task_id}",
        task_id=task_id,
        title="Original Mission",
        description="Original description",
        phases=[
            MissionPhase(
                phase_id="phase-1",
                title="Phase 1",
                description="Original phase",
                feature_ids=["feature-keep"],
                validation_contract_ids=["VAL-KEEP"],
            )
        ],
        features=[
            MissionFeature(
                feature_id="feature-keep",
                hunger_item_id="H-KEEP",
                phase_id="phase-1",
                title="Keep feature",
                description="Existing feature",
            )
        ],
        created_at=datetime.now(timezone.utc),
    )
    ctx.repo.save_mission(mission)
    ctx.repo.save_validation_contract(
        ValidationContract(
            mission_id=mission.mission_id,
            assertions=[
                ValidationAssertion(
                    assertion_id="VAL-KEEP",
                    phase_id="phase-1",
                    title="Keep assertion",
                    description="Existing assertion",
                    check_type="behavioral_assertion",
                )
            ],
        )
    )
    ctx.repo.save_hunger_ledger(task_id, HungerLedger(task_id=task_id, items=[]))


def _write_update_dir(tmp_path: Path, *, assertion_phase_id: str = "phase-1") -> Path:
    mission_dir = tmp_path / "updated"
    mission_dir.mkdir()
    (mission_dir / "mission.md").write_text(
        "\n".join(
            [
                "# Updated Mission",
                "",
                "## Description",
                "",
                "Updated description.",
                "",
                "## Phases",
                "",
                "### phase-1 Phase 1",
                "",
                "Features:",
                "- [pending] Feature feature-new: New feature (hunger: H-NEW)",
                "",
                "Assertions:",
                "- [pending] Assertion VAL-NEW: New assertion (behavioral_assertion)",
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
                        "feature_id": "feature-new",
                        "hunger_item_id": "H-NEW",
                        "phase_id": "phase-1",
                        "title": "New feature",
                        "description": "Imported feature",
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
                        "assertion_id": "VAL-NEW",
                        "phase_id": assertion_phase_id,
                        "title": "New assertion",
                        "description": "Imported assertion",
                        "check_type": "behavioral_assertion",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (mission_dir / "services.yaml").write_text(
        yaml.safe_dump(
            {"commands": {"test": ".venv/bin/pytest -q"}, "services": {}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return mission_dir


def _sql_counts(db_path: Path) -> tuple[int, int, int, int]:
    with sqlite3.connect(str(db_path)) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mission_phases").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mission_features").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM validation_assertions").fetchone()[0],
        )


def test_running_rejects_import_with_event_and_no_sqlite_writes(tmp_path: Path) -> None:
    ctx, db_path = _context(tmp_path)
    _seed_mission(ctx)
    before_counts = _sql_counts(db_path)

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(_write_update_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code == 7
    assert "mission import requires HUMAN_PAUSED state" in result.output
    assert _sql_counts(db_path) == before_counts
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='MISSION_IMPORT_REJECTED'"
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM evidence
            WHERE json_extract(payload_json, '$.kind')='mission_import'
            """
        ).fetchone()[0] == 0


def test_paused_accepts_import_evidence_and_leaves_best_mirrors(tmp_path: Path) -> None:
    ctx, db_path = _context(tmp_path)
    _seed_mission(ctx)
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )
    best = tmp_path / "tasks" / "T-import" / "best" / "files"
    best.mkdir(parents=True)
    mirror_files = [
        best / "mission.md",
        best / "features.yaml",
        best / "validation-contract.yaml",
        best / "services.yaml",
    ]
    for mirror in mirror_files:
        mirror.write_text(f"unchanged {mirror.name}\n", encoding="utf-8")
    before_mirrors = {mirror.name: mirror.read_text(encoding="utf-8") for mirror in mirror_files}

    result = CliRunner().invoke(
        cli,
        ["mission", "import", "T-import", "--from", str(_write_update_dir(tmp_path))],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "1 features added, 1 assertions added" in result.output
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_features").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM validation_assertions").fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM evidence
            WHERE evidence_type='human_input'
              AND json_extract(payload_json, '$.kind')='mission_import'
            """
        ).fetchone()[0] == 1
    assert {
        mirror.name: mirror.read_text(encoding="utf-8") for mirror in mirror_files
    } == before_mirrors


def test_paused_import_with_unknown_assertion_phase_exits_2_without_fk_error(
    tmp_path: Path,
) -> None:
    ctx, db_path = _context(tmp_path)
    _seed_mission(ctx)
    ctx.repo.save_stop_report(
        StopReport(
            task_id="T-import",
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )
    before_counts = _sql_counts(db_path)

    result = CliRunner().invoke(
        cli,
        [
            "mission",
            "import",
            "T-import",
            "--from",
            str(
                _write_update_dir(
                    tmp_path,
                    assertion_phase_id="phase-missing",
                )
            ),
        ],
        obj=ctx,
    )

    assert result.exit_code == 2
    assert "VAL-NEW" in result.output
    assert "phase-missing" in result.output
    assert "IntegrityError" not in result.output
    assert "FOREIGN KEY constraint failed" not in result.output
    assert _sql_counts(db_path) == before_counts
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='MISSION_LOAD_FAILED'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='MISSION_IMPORT_FAILED'"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM evidence
            WHERE evidence_type='human_input'
              AND json_extract(payload_json, '$.kind')='mission_import'
            """
        ).fetchone()[0] == 0
