"""Cross-restart parity for memory lifecycle (PRD §19 / E0-13).

Process P1 approves a candidate via the CLI; process P2 reopens the
SQLite database and reads the PromotedMemory + the candidate's
post-approval state. Persistence-only — no orchestrator, no async.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.memory import MemoryCandidate
from hungerloop.repository.sqlite_repo import SQLiteRepository


def test_approve_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    workspace_root = tmp_path / "workspaces"

    # ---- P1: open DB, seed candidate, run `memory approve` -------------
    p1 = SQLiteRepository.open(db)
    p1.create_task("t1", "Goal")
    p1.save_memory_candidate(
        MemoryCandidate(
            candidate_id="cand-1",
            task_id="t1",
            content="Verified acceptance check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            action_verified=True,
            traceable=True,
            reusable=True,
        )
    )
    p1_ctx = CliContext(repo=p1, workspace_root=workspace_root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "approve", "cand-1", "--reviewer", "alice"],
        obj=p1_ctx,
    )
    assert result.exit_code == 0, result.output
    p1.close() if hasattr(p1, "close") else None

    # ---- P2: reopen DB, verify everything survived --------------------
    p2 = SQLiteRepository.open(db)
    cand = p2.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.state == "approved"
    assert cand.reviewer == "alice"
    assert cand.reviewed_at is not None

    promoted = p2.list_promoted_memories("t1")
    assert len(promoted) == 1
    assert promoted[0].source_candidate_id == "cand-1"
    assert promoted[0].approved_by == "alice"
    assert promoted[0].evidence_ids == ["ev-1"]
    assert promoted[0].accepted_check_keys == ["H-001:0"]

    # Audit events present.
    types = {ev["event_type"] for ev in p2.list_events("t1")}
    assert "memory_candidate_approved" in types
    assert "memory_promoted" in types


def test_reject_persists_reason_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    workspace_root = tmp_path / "workspaces"

    p1 = SQLiteRepository.open(db)
    p1.create_task("t1", "Goal")
    p1.save_memory_candidate(
        MemoryCandidate(
            candidate_id="cand-1",
            task_id="t1",
            content="example",
            evidence_ids=["ev-1"],
        )
    )
    p1_ctx = CliContext(repo=p1, workspace_root=workspace_root)
    CliRunner().invoke(
        cli,
        ["memory", "reject", "cand-1", "--reason", "out of scope"],
        obj=p1_ctx,
    )

    p2 = SQLiteRepository.open(db)
    cand = p2.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.state == "rejected"
    assert cand.rejection_reason == "out of scope"


def test_defer_then_approve_round_trips_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "hungerloop.sqlite"
    workspace_root = tmp_path / "workspaces"

    p1 = SQLiteRepository.open(db)
    p1.create_task("t1", "Goal")
    p1.save_memory_candidate(
        MemoryCandidate(
            candidate_id="cand-1",
            task_id="t1",
            content="Verified acceptance check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            action_verified=True,
            traceable=True,
            reusable=True,
        )
    )
    runner = CliRunner()
    runner.invoke(
        cli,
        ["memory", "defer", "cand-1"],
        obj=CliContext(repo=p1, workspace_root=workspace_root),
    )

    p2 = SQLiteRepository.open(db)
    assert p2.get_memory_candidate("cand-1").state == "deferred"  # type: ignore[union-attr]

    runner.invoke(
        cli,
        ["memory", "approve", "cand-1"],
        obj=CliContext(repo=p2, workspace_root=workspace_root),
    )
    p3 = SQLiteRepository.open(db)
    cand = p3.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.state == "approved"
    assert p3.list_promoted_memories("t1")
