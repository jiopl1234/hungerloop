"""Editor-flow tests for ``hungerloop mission edit``."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.enums import StopReason
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository


def _set_task_status(ctx: CliContext, task_id: str, status: str) -> None:
    task = ctx.repo.get_task(task_id)
    assert task is not None
    if isinstance(ctx.repo, InMemoryRepository):
        ctx.repo._tasks[task_id] = task.model_copy(update={"status": status})
        return
    assert isinstance(ctx.repo, SQLiteRepository)
    ctx.repo.conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
        (status, task.updated_at, task_id),
    )


def _set_task_human_paused(ctx: CliContext, task_id: str) -> None:
    _set_task_status(ctx, task_id, "HUMAN_PAUSED")


def _context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


def _sqlite_context(tmp_path: Path) -> tuple[CliContext, Path]:
    db_path = tmp_path / "hungerloop.sqlite"
    return CliContext(repo=SQLiteRepository.open(db_path), workspace_root=tmp_path), db_path


def _mission_markdown(title: str = "Editable Mission") -> str:
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Description",
            "",
            "Original description.",
            "",
            "## Phases",
            "",
            "### phase_1 Build",
            "",
            "Status: `pending`",
            "",
            "Build phase.",
            "",
            "Features:",
            "- [pending] Feature feat_1: Build report (hunger: H-001)",
            "",
            "Assertions:",
            "- [pending] Assertion VAL-001: Report exists (behavioral_assertion)",
            "",
        ]
    )


def _seed_paused_mission(ctx: CliContext, task_id: str = "T-edit") -> Mission:
    ctx.repo.create_task(task_id, "Edit mission")
    mission = Mission(
        mission_id=f"mission-{task_id}",
        task_id=task_id,
        title="Editable Mission",
        description="Original description.",
        phases=[
            MissionPhase(
                phase_id="phase_1",
                title="Build",
                description="Build phase.",
                feature_ids=["feat_1"],
                validation_contract_ids=["VAL-001"],
            )
        ],
        features=[
            MissionFeature(
                feature_id="feat_1",
                hunger_item_id="H-001",
                phase_id="phase_1",
                title="Build report",
                description="Create report.md",
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
                    assertion_id="VAL-001",
                    phase_id="phase_1",
                    title="Report exists",
                    description="report.md should exist",
                    check_type="behavioral_assertion",
                )
            ],
        )
    )
    ctx.repo.save_stop_report(
        StopReport(
            task_id=task_id,
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )
    _set_task_human_paused(ctx, task_id)
    best = ctx.workspace_root / "tasks" / task_id / "best" / "files"
    best.mkdir(parents=True)
    (best / "mission.md").write_text(_mission_markdown(), encoding="utf-8")
    return mission


def test_edit_requires_task_record_status_human_paused(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_paused_mission(ctx)
    _set_task_status(ctx, "T-edit", "stopped")
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
    assert all(
        event["event_type"] != "MISSION_EDIT_CANCELLED"
        for event in ctx.repo.list_events("T-edit")
    )


def test_edit_allows_when_task_record_status_human_paused(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_paused_mission(ctx)
    editor = tmp_path / "editor.py"
    editor.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "from pathlib import Path",
                "Path(sys.argv[1]).read_text(encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    editor.chmod(0o755)

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": str(editor)},
    )

    assert result.exit_code == 0, result.output
    assert "0 features added, 0 assertions added" in result.output


def _mission_state_dump(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "missions",
        "mission_phases",
        "mission_features",
        "validation_assertions",
        "hunger_items",
        "hunger_ledgers",
        "evidence",
    )
    with sqlite3.connect(str(db_path)) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }


def test_edit_invokes_import_and_records_applied_event(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _seed_paused_mission(ctx)
    captured = tmp_path / "captured.md"
    editor = tmp_path / "editor.py"
    editor.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import sys",
                f"captured = Path({str(captured)!r})",
                "path = Path(sys.argv[1])",
                "captured.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    editor.chmod(0o755)

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": str(editor)},
    )

    assert result.exit_code == 0, result.output
    assert captured.read_text(encoding="utf-8") == _mission_markdown()
    assert "0 features added, 0 assertions added" in result.output
    events = ctx.repo.list_events("T-edit")
    applied_events = [
        event["event_type"]
        for event in events
        if event["event_type"] == "MISSION_IMPORT_APPLIED"
    ]
    assert applied_events == ["MISSION_IMPORT_APPLIED"]
    import_evidence = [
        row
        for row in ctx.repo._evidence.values()
        if row.get("kind") == "mission_import"
    ]
    assert len(import_evidence) == 1


def test_edit_editor_nonzero_cancels_without_mission_writes(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    mission = _seed_paused_mission(ctx)
    before = (
        ctx.repo.get_mission("T-edit"),
        ctx.repo.list_mission_features(mission_id=mission.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission.mission_id),
    )

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": "/usr/bin/false"},
    )

    assert result.exit_code != 0
    after = (
        ctx.repo.get_mission("T-edit"),
        ctx.repo.list_mission_features(mission_id=mission.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission.mission_id),
    )
    assert after == before
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_EDIT_CANCELLED"
    ] == ["MISSION_EDIT_CANCELLED"]
    assert not [
        row for row in ctx.repo._evidence.values() if row.get("kind") == "mission_import"
    ]


def test_edit_editor_nonzero_preserves_sqlite_mission_tables(
    tmp_path: Path,
) -> None:
    ctx, db_path = _sqlite_context(tmp_path)
    _seed_paused_mission(ctx)
    before = _mission_state_dump(db_path)

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": "/usr/bin/false"},
    )

    assert result.exit_code != 0
    assert _mission_state_dump(db_path) == before
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_EDIT_CANCELLED"
    ] == ["MISSION_EDIT_CANCELLED"]


def test_edit_empty_buffer_cancels_without_mission_writes(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    mission = _seed_paused_mission(ctx)
    before = (
        ctx.repo.get_mission("T-edit"),
        ctx.repo.list_mission_features(mission_id=mission.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission.mission_id),
    )
    editor = tmp_path / "empty_editor.py"
    editor.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import sys",
                "Path(sys.argv[1]).write_text('', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    editor.chmod(0o755)

    result = CliRunner().invoke(
        cli,
        ["mission", "edit", "T-edit"],
        obj=ctx,
        env={"EDITOR": str(editor)},
    )

    assert result.exit_code != 0
    after = (
        ctx.repo.get_mission("T-edit"),
        ctx.repo.list_mission_features(mission_id=mission.mission_id),
        ctx.repo.list_validation_assertions(mission_id=mission.mission_id),
    )
    assert after == before
    assert "mission edit cancelled: empty buffer" in result.output
    assert [
        event["event_type"]
        for event in ctx.repo.list_events("T-edit")
        if event["event_type"] == "MISSION_EDIT_CANCELLED"
    ] == ["MISSION_EDIT_CANCELLED"]
