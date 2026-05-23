from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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


def _seed_mission_task(ctx: CliContext, task_id: str) -> None:
    ctx.repo.create_task(task_id, "Build report")
    ctx.repo.save_mission(
        Mission(
            mission_id=f"mission-{task_id}",
            task_id=task_id,
            title="Mission",
            description="Mission description",
            phases=[
                MissionPhase(
                    phase_id="phase-1",
                    title="Phase",
                    description="Phase description",
                    feature_ids=["feature-1"],
                    status="in_progress",
                )
            ],
            features=[
                MissionFeature(
                    feature_id="feature-1",
                    hunger_item_id="H-001",
                    phase_id="phase-1",
                    title="Feature",
                    description="Feature description",
                    status="pending",
                )
            ],
            created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        )
    )
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


def test_rollback_flag(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx = _context(tmp_path)
    task_id = "T-runtime-flag"
    _seed_mission_task(ctx, task_id)
    monkeypatch.setenv("HUNGERLOOP_MISSION_RUNTIME", "0")

    result = CliRunner().invoke(
        cli,
        ["mission", "run", task_id, "--max-loops", "2"],
        obj=ctx,
    )

    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output
    mission_events = [
        event
        for event in ctx.repo.list_events(task_id)
        if str(event["event_type"]).startswith("mission.")
    ]
    assert mission_events == []
