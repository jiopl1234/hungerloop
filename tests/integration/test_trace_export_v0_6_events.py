"""Integration coverage for v0.6 mission-runtime trace export events."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.events import EventType
from hungerloop.repository.sqlite_repo import SQLiteRepository


def test_trace_export_includes_all_present_v0_6_event_types(tmp_path: Path) -> None:
    """Export the DB event stream generically without a per-event allow-list."""
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    task_id = "T-trace"
    repo.create_task(task_id, "Trace every v0.6 event family")
    expected = _v0_6_mission_runtime_event_types()
    for event_type in expected:
        event_name = event_type.value if isinstance(event_type, EventType) else event_type
        repo.append_event(
            event_type,
            {
                "mission_id": "mission-1",
                "phase_id": "phase-1",
                "feature_id": "feature-1",
                "assignment_id": "assignment-1",
                "event_name": event_name,
            },
            task_id=task_id,
            loop_id=1,
        )
    with sqlite3.connect(db_path) as conn:
        sqlite_event_types = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT event_type FROM events WHERE task_id=?",
                (task_id,),
            ).fetchall()
        }

    result = CliRunner().invoke(
        cli,
        ["trace", "export", task_id],
        obj=CliContext(repo=repo, workspace_root=tmp_path),
    )

    assert result.exit_code == 0, result.output
    exported = [
        json.loads(line) for line in result.output.splitlines() if line.strip()
    ]
    exported_event_types = {row["event_type"] for row in exported}
    assert sqlite_event_types <= exported_event_types
    assert {
        event_type.value if isinstance(event_type, EventType) else event_type
        for event_type in expected
    } <= exported_event_types
    assignment_events = [
        row for row in exported if row["event_type"].startswith("worker.assignment_")
    ]
    assert assignment_events
    assert all(
        row["payload"]["mission_id"] == "mission-1" for row in assignment_events
    )


def _v0_6_mission_runtime_event_types() -> list[EventType | str]:
    prefixes = (
        "mission.created",
        "mission.phase_",
        "mission.feature_",
        "worker.assignment_",
        "worker.handoff_",
        "validation.scrutiny_",
        "validation.user_testing_",
        "validation.assertion_",
    )
    return [
        *[
            event_type
            for event_type in EventType
            if event_type.value.startswith(prefixes)
        ],
        EventType.MISSION_STATE_REGENERATED,
        # This forward-compatible raw event verifies trace export does not
        # depend on EventType membership or any hardcoded event allow-list.
        "worker.handoff_processed",
    ]
