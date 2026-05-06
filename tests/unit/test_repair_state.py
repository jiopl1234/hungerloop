"""``hungerloop repair-state`` divergence catalog tests (PRD §16.3).

Pins the seven D-codes from §16.3:

* D1 clean → empty list, exit 0
* D2 hash mismatch / D3 missing file → corruption, refuse
* D4 missing manifest → warn, fix rewrites the manifest
* D5 orphan candidate → warn, fix moves it under ``rejected/``
* D6 stale lock → warn, refuse and direct to ``--steal-lock``
* D7 dangling validation row → warn, refuse (deferred to v0.5b.1)

Plus a guarantee that ``--fix`` never deletes or rewrites files inside
``best/files/`` (PRD §16.3 invariant).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.tracing import LoopTrace
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.repair_state import (
    RepairStateService,
)
from hungerloop.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


@pytest.fixture
def service(context: CliContext) -> RepairStateService:
    return RepairStateService(
        repo=context.repo,
        workspace_root=context.workspace_root,
    )


def _seed_best(ws: WorkspaceManager, task_id: str, files: dict[str, str]) -> None:
    """Seed ``best/files/`` and write a manifest with per-file hashes.

    Goes through the production path (create_candidate → promote) so the
    manifest's ``files`` dict is populated by ``WorkspaceManager`` exactly
    as it will be in real loops, then deletes the residual candidate dir
    so D5 detection doesn't fire on the seeding artefact.
    """
    candidate = ws.create_candidate_workspace(task_id, loop_id=1)
    for rel, body in files.items():
        target = candidate / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    ws.promote_candidate_to_best(task_id, loop_id=1)
    # Clean up the candidate dir so the seed itself doesn't look like a D5
    # orphan (post-promotion the candidates/loop_001/ tree still exists on
    # disk in the real flow too — but a real loop would have a LoopTrace).
    shutil.rmtree(ws.task_root(task_id) / "candidates", ignore_errors=True)


def _read_manifest(ws: WorkspaceManager, task_id: str) -> dict[str, Any]:
    return json.loads(
        (ws.best_files_dir(task_id).parent / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


def test_detect_d1_clean_state_returns_empty(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha", "sub/b.txt": "beta"})
    assert service.detect("task-1") == []


def test_detect_d2_hash_mismatch_marks_corruption(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    # Mutate the file under best/ without updating the manifest.
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    divs = service.detect("task-1")
    assert len(divs) == 1
    assert divs[0].kind == "D2"
    assert divs[0].corruption is True


def test_detect_d3_missing_file_marks_corruption(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha", "b.txt": "beta"})
    (ws.best_files_dir("task-1") / "b.txt").unlink()
    divs = service.detect("task-1")
    assert len(divs) == 1
    assert divs[0].kind == "D3"
    assert divs[0].corruption is True


def test_detect_d4_missing_manifest_warns_only(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()
    divs = service.detect("task-1")
    assert len(divs) == 1
    assert divs[0].kind == "D4"
    assert divs[0].corruption is False


def test_detect_d4_extra_files_on_disk_warns_only(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    # Drop a new file under best/ without rewriting the manifest.
    (ws.best_files_dir("task-1") / "drift.txt").write_text(
        "untracked", encoding="utf-8"
    )
    divs = service.detect("task-1")
    assert all(d.kind == "D4" and not d.corruption for d in divs)
    assert any(d.target.endswith("drift.txt") for d in divs)


def test_detect_d5_orphan_candidate_warns_only(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    # An orphan candidate dir, no LoopTrace + no _candidates row.
    ws.create_candidate_workspace("task-1", loop_id=7)
    divs = service.detect("task-1")
    assert len(divs) == 1
    assert divs[0].kind == "D5"
    assert "loop_007" in divs[0].detail


def test_detect_d5_skipped_when_loop_trace_exists(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    ws.create_candidate_workspace("task-1", loop_id=7)
    context.repo.save_loop_trace(
        LoopTrace(
            task_id="task-1",
            loop_id=7,
            phase=LoopPhase.EXPLORE.value,
            active_hunger=80.0,
            drive_budget=80.0,
            work_pressure=10.0,
            committed=False,
            delta_summary="ran",
            next_action="continue",
        )
    )
    assert service.detect("task-1") == []


def test_detect_d5_skipped_when_candidate_row_exists(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    ws.create_candidate_workspace("task-1", loop_id=7)
    context.repo.save_candidate(
        CandidateState(
            id="cand-7",
            task_id="task-1",
            loop_id=7,
            summary="seeded",
            workspace_ref="candidates/loop_007",
        )
    )
    assert service.detect("task-1") == []


def test_detect_d6_stale_lock_warns_only(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    repo: Any = context.repo
    repo._task_locks["task-1"] = {
        "owner": "h:1234:abcd",
        "locked_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    service = RepairStateService(
        repo=repo,
        workspace_root=context.workspace_root,
        stale_threshold_seconds=60 * 30,
    )
    divs = service.detect("task-1")
    assert len(divs) == 1
    assert divs[0].kind == "D6"
    assert divs[0].corruption is False
    assert "stale" in divs[0].detail or "task lock" in divs[0].detail


def test_detect_d6_skipped_under_threshold(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    repo: Any = context.repo
    repo._task_locks["task-1"] = {
        "owner": "h:1234:abcd",
        "locked_at": datetime.now(timezone.utc) - timedelta(seconds=10),
    }
    service = RepairStateService(
        repo=repo,
        workspace_root=context.workspace_root,
        stale_threshold_seconds=60 * 30,
    )
    assert service.detect("task-1") == []


def test_detect_d7_dangling_accepted_check_warns_only(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    context.repo.save_accepted_check(
        task_id="task-1",
        check_key="H-001:0",
        hunger_item_id="H-001",
        check_index=0,
        accepted_at_loop=1,
        validation_id="missing-vid",
        evidence_id=None,
    )
    divs = service.detect("task-1")
    assert any(d.kind == "D7" and not d.corruption for d in divs)


def test_detect_d7_skipped_when_validation_exists(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    context.repo.save_validation_report(
        ValidationReport(
            id="vid-1",
            task_id="task-1",
            loop_id=1,
            candidate_state_id="cand-1",
            baseline_state_id=None,
            verdict=ValidationVerdict.PASS,
        )
    )
    context.repo.save_accepted_check(
        task_id="task-1",
        check_key="H-001:0",
        hunger_item_id="H-001",
        check_index=0,
        accepted_at_loop=1,
        validation_id="vid-1",
        evidence_id=None,
    )
    assert service.detect("task-1") == []


# ---------------------------------------------------------------------------
# apply_fix()
# ---------------------------------------------------------------------------


def test_fix_d4_rewrites_manifest_and_emits_event(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()

    divs = service.detect("task-1")
    assert len(divs) == 1
    outcome = service.apply_fix(divs[0])
    assert outcome.fixed is True

    manifest = _read_manifest(ws, "task-1")
    expected_hash = hashlib.sha256(b"alpha").hexdigest()
    assert manifest["files"]["a.txt"] == expected_hash

    repo: Any = context.repo
    repair_events = [
        e for e in repo._events if e["event_type"] == "repair_state_action"
    ]
    assert len(repair_events) == 1
    assert repair_events[0]["payload"]["kind"] == "D4"
    assert repair_events[0]["payload"]["action"] == "fix"

    # A second --check pass sees a clean state.
    assert service.detect("task-1") == []


def test_fix_d5_moves_orphan_to_rejected(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    candidate = ws.create_candidate_workspace("task-1", loop_id=7)
    assert candidate.exists()

    divs = service.detect("task-1")
    [d5] = [d for d in divs if d.kind == "D5"]
    outcome = service.apply_fix(d5)
    assert outcome.fixed is True

    rejected = ws.task_root("task-1") / "rejected" / "loop_007" / "files"
    assert rejected.exists()
    assert not (
        ws.task_root("task-1") / "candidates" / "loop_007" / "files"
    ).exists()


def test_fix_d2_refuses_with_corruption_summary(
    context: CliContext, service: RepairStateService
) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    [div] = service.detect("task-1")
    outcome = service.apply_fix(div)
    assert outcome.fixed is False
    assert "backup" in outcome.summary.lower()


def test_fix_d6_refuses_and_directs_to_steal_lock(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    repo: Any = context.repo
    repo._task_locks["task-1"] = {
        "owner": "h:1234:abcd",
        "locked_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    service = RepairStateService(
        repo=repo,
        workspace_root=context.workspace_root,
        stale_threshold_seconds=60 * 30,
    )
    [div] = service.detect("task-1")
    outcome = service.apply_fix(div)
    assert outcome.fixed is False
    assert "--steal-lock" in outcome.summary


def test_fix_never_deletes_or_overwrites_files_in_best(
    context: CliContext,
) -> None:
    """Snapshot best/ before, run every fix path, assert byte-equal after."""
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha", "sub/b.txt": "beta"})

    # Snapshot every byte before we exercise fixes.
    best_dir = ws.best_files_dir("task-1")
    snapshot: dict[str, bytes] = {}
    for f in best_dir.rglob("*"):
        if f.is_file():
            snapshot[str(f.relative_to(best_dir))] = f.read_bytes()

    # Stage every fix-able divergence variant.
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()  # D4
    ws.create_candidate_workspace("task-1", loop_id=7)  # D5

    service = RepairStateService(
        repo=context.repo, workspace_root=context.workspace_root
    )
    divs = service.detect("task-1")
    for d in divs:
        service.apply_fix(d)

    after: dict[str, bytes] = {}
    for f in best_dir.rglob("*"):
        if f.is_file():
            after[str(f.relative_to(best_dir))] = f.read_bytes()

    assert snapshot == after


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_check_clean_exit_0(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--check"], obj=context)
    assert result.exit_code == 0
    assert "clean" in result.output


def test_cli_check_warns_exit_1(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()  # D4
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--check"], obj=context)
    assert result.exit_code == 1
    assert "WARN" in result.output
    assert "D4" in result.output


def test_cli_check_corruption_exit_2(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--check"], obj=context)
    assert result.exit_code == 2
    assert "CORRUPT" in result.output
    assert "D2" in result.output


def test_cli_check_mixed_warning_and_corruption_exits_2(
    context: CliContext,
) -> None:
    """When BOTH a warning and a corruption divergence are present,
    --check must surface the corruption exit code (2), not silently
    demote it to a warning (1) — corruption is the more dangerous
    signal and CI pipelines hinge on the distinction.
    """
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    # D2 corruption: tamper with the file.
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    # D5 warning: orphan candidate dir.
    ws.create_candidate_workspace("task-1", loop_id=7)
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--check"], obj=context)
    assert result.exit_code == 2
    assert "D2" in result.output
    assert "D5" in result.output


def test_cli_fix_corruption_exit_2(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--fix"], obj=context)
    assert result.exit_code == 2
    assert "refusing" in result.output.lower()


def test_cli_fix_no_actionable_exit_3(context: CliContext) -> None:
    """D6/D7 alone aren't auto-fixable in v0.5b.0; CLI should exit 3."""
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    repo: Any = context.repo
    repo._task_locks["task-1"] = {
        "owner": "h:1234:abcd",
        "locked_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["repair-state", "task-1", "--fix", "--lock-stale-sec", "1800"],
        obj=context,
    )
    assert result.exit_code == 3
    assert "no auto-fixable" in result.output.lower()


def test_cli_requires_check_or_fix(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1"], obj=context)
    assert result.exit_code != 0
    assert "--check" in result.output or "--fix" in result.output


def test_cli_fix_d4_succeeds(context: CliContext) -> None:
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--fix"], obj=context)
    assert result.exit_code == 0
    assert "fixed" in result.output
    # manifest is back; subsequent --check is clean.
    result2 = runner.invoke(
        cli, ["repair-state", "task-1", "--check"], obj=context
    )
    assert result2.exit_code == 0


# ---------------------------------------------------------------------------
# D8-D13 detectors (PRD §13 / D1-04..09)
# ---------------------------------------------------------------------------


def test_detect_d8_accepted_check_missing_validation(
    context: CliContext, service: RepairStateService
) -> None:
    """D8 — accepted_checks references a validation_id that was never
    persisted. Same predicate as D7 but tagged corruption so --check
    exits 2."""
    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_accepted_check(
        task_id="task-1",
        check_key="H-001:0",
        hunger_item_id="H-001",
        check_index=0,
        accepted_at_loop=1,
        validation_id="VAL-missing",
        evidence_id=None,
    )
    divs = service.detect("task-1")
    d8 = [d for d in divs if d.kind == "D8"]
    assert d8, "expected a D8 row when validation_id is dangling"
    assert d8[0].corruption is True


def test_detect_d9_best_state_missing_validation(
    context: CliContext, service: RepairStateService
) -> None:
    """D9 — best_state.validation_id references a missing validation row."""
    from hungerloop.models.blackboard import BestState

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_best_state(
        BestState(
            task_id="task-1",
            state_id="STATE-1",
            summary="seed",
            validation_id="VAL-gone",
        )
    )
    divs = service.detect("task-1")
    d9 = [d for d in divs if d.kind == "D9"]
    assert d9
    assert d9[0].corruption is True


def test_detect_d10_stopped_no_stop_report(
    context: CliContext, service: RepairStateService
) -> None:
    """D10 — task row marked stopped but no StopReport persisted.

    Construct the divergent state directly: create the task and
    manually flip its status to ``stopped`` without a StopReport.
    """
    repo = context.repo
    repo.create_task("task-1", "Goal")
    # Directly mutate the in-memory TaskRecord to mark stopped.
    task = repo.get_task("task-1")
    assert task is not None
    repo._tasks["task-1"] = task.model_copy(update={"status": "stopped"})  # type: ignore[attr-defined]

    divs = service.detect("task-1")
    d10 = [d for d in divs if d.kind == "D10"]
    assert d10
    assert d10[0].corruption is False


def test_detect_d11_usage_drift_above_threshold(
    context: CliContext, service: RepairStateService
) -> None:
    """D11 — usage_snapshot diverges from evidence sum by more than 5%."""
    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_model_call_as_evidence(
        task_id="task-1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.10,
        response_preview="{}",
    )
    # Stomp the snapshot with a wildly-wrong number to trigger drift.
    from hungerloop.models.usage import UsageSnapshot

    repo.save_usage_snapshot(
        UsageSnapshot(
            task_id="task-1",
            tokens=10,  # evidence sum is 300
            cost_usd=0.01,  # evidence sum is 0.10
            llm_calls=1,
            tool_calls=0,
        )
    )
    divs = service.detect("task-1")
    d11 = [d for d in divs if d.kind == "D11"]
    assert d11


def test_detect_d11_usage_in_tolerance(
    context: CliContext, service: RepairStateService
) -> None:
    """D11 — within-5% drift does not fire."""
    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_model_call_as_evidence(
        task_id="task-1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=100,
        cost_usd=0.10,
        response_preview="{}",
    )
    # save_model_call_as_evidence already bumped the snapshot to the
    # canonical totals. Detection should be clean.
    divs = service.detect("task-1")
    assert not [d for d in divs if d.kind == "D11"]


def test_detect_d12_terminal_candidate_with_workspace(
    context: CliContext, service: RepairStateService
) -> None:
    """D12 — candidate row marked rejected but workspace dir still in
    candidates/ (real moves go to rejected/)."""
    ws = WorkspaceManager(context.workspace_root)
    repo = context.repo
    repo.create_task("task-1", "Goal")
    candidate_path = ws.create_candidate_workspace("task-1", loop_id=1)
    cand = CandidateState(
        id="cand-1",
        task_id="task-1",
        loop_id=1,
        summary="x",
        workspace_ref=str(candidate_path),
    )
    repo.save_candidate(cand)
    repo.mark_candidate_rejected("cand-1")

    divs = service.detect("task-1")
    d12 = [d for d in divs if d.kind == "D12"]
    assert d12


def test_detect_d13_event_with_no_loop_trace(
    context: CliContext, service: RepairStateService
) -> None:
    """D13 — event references loop_id with no LoopTrace row."""
    from hungerloop.models.events import EventType

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.append_event(
        EventType.LOOP_STARTED, {}, task_id="task-1", loop_id=42
    )
    # No LoopTrace saved for loop 42 → orphan.
    divs = service.detect("task-1")
    d13 = [d for d in divs if d.kind == "D13"]
    assert d13


# ---------------------------------------------------------------------------
# D10 / D11 fixes (PRD §13 / FR-12)
# ---------------------------------------------------------------------------


def test_apply_fix_d10_synthesises_stop_report(
    context: CliContext, service: RepairStateService
) -> None:
    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_loop_trace(
        LoopTrace(
            task_id="task-1",
            loop_id=7,
            phase="explore",
            active_hunger=10.0,
            drive_budget=10.0,
            work_pressure=5.0,
            committed=False,
        )
    )
    task = repo.get_task("task-1")
    assert task is not None
    repo._tasks["task-1"] = task.model_copy(update={"status": "stopped"})  # type: ignore[attr-defined]

    divs = [d for d in service.detect("task-1") if d.kind == "D10"]
    assert divs
    outcome = service.apply_fix(divs[0])
    assert outcome.fixed is True
    last = repo.get_last_stop_report("task-1")
    assert last is not None
    assert last.last_loop_id == 7
    assert "auto-recovered" in last.recommendation


def test_apply_fix_d11_recomputes_usage(
    context: CliContext, service: RepairStateService
) -> None:
    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_model_call_as_evidence(
        task_id="task-1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.10,
        response_preview="{}",
    )
    from hungerloop.models.usage import UsageSnapshot

    repo.save_usage_snapshot(
        UsageSnapshot(
            task_id="task-1",
            tokens=10,
            cost_usd=0.01,
            llm_calls=1,
            tool_calls=0,
        )
    )
    divs = [d for d in service.detect("task-1") if d.kind == "D11"]
    assert divs
    outcome = service.apply_fix(divs[0])
    assert outcome.fixed is True
    snap = repo.get_usage_snapshot("task-1")
    # Recomputed totals match the evidence row.
    assert snap.tokens == 300
    assert abs(snap.cost_usd - 0.10) < 1e-6
    assert snap.llm_calls == 1


# ---------------------------------------------------------------------------
# CLI --apply / --fix orchestration (D1-10)
# ---------------------------------------------------------------------------


def test_cli_apply_alias_works(context: CliContext) -> None:
    """``--apply`` is the alias for ``--fix`` introduced in v0.5d.1."""
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    (ws.best_files_dir("task-1").parent / "manifest.json").unlink()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["repair-state", "task-1", "--apply"], obj=context
    )
    assert result.exit_code == 0, result.output
    assert "fixed" in result.output


def test_cli_fix_mixed_corruption_and_warning_continues_then_exits_2(
    context: CliContext,
) -> None:
    """FR-13: fix the warning rows, refuse corruption, exit 2.

    Mixed fixture: D2 (hash mismatch — corruption, refuse) plus D5
    (orphan candidate dir — warning, fixable). The CLI should fix D5
    and refuse D2 in the same invocation, then exit 2 because at
    least one corruption refusal happened.
    """
    ws = WorkspaceManager(context.workspace_root)
    _seed_best(ws, "task-1", {"a.txt": "alpha"})
    # Corruption: tamper a.txt without updating the manifest hash.
    (ws.best_files_dir("task-1") / "a.txt").write_text(
        "tampered", encoding="utf-8"
    )
    # Warning: orphan candidate workspace (no LoopTrace, no candidate row).
    ws.create_candidate_workspace("task-1", loop_id=42)

    runner = CliRunner()
    result = runner.invoke(cli, ["repair-state", "task-1", "--fix"], obj=context)
    assert result.exit_code == 2, result.output
    assert "fixed D5" in result.output
    assert "skip  D2" in result.output
    assert "summary:" in result.output


# ---------------------------------------------------------------------------
# trace export --include-global (D1-11)
# ---------------------------------------------------------------------------


def test_cli_trace_export_excludes_global_by_default(
    context: CliContext,
) -> None:
    from hungerloop.models.events import EventType

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.append_event(
        EventType.LOOP_STARTED, {"x": 1}, task_id="task-1", loop_id=1
    )
    # Global event — task_id IS NULL.
    repo.append_event(EventType.UNKNOWN_MODEL_PRICING, {"model": "x"})

    runner = CliRunner()
    result = runner.invoke(
        cli, ["trace", "export", "task-1"], obj=context
    )
    assert result.exit_code == 0
    assert "loop_started" in result.output
    assert "unknown_model_pricing" not in result.output


def test_cli_trace_export_include_global(context: CliContext) -> None:
    from hungerloop.models.events import EventType

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.append_event(
        EventType.LOOP_STARTED, {"x": 1}, task_id="task-1", loop_id=1
    )
    repo.append_event(EventType.UNKNOWN_MODEL_PRICING, {"model": "x"})

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["trace", "export", "task-1", "--include-global"],
        obj=context,
    )
    assert result.exit_code == 0
    assert "loop_started" in result.output
    assert "unknown_model_pricing" in result.output


# ---------------------------------------------------------------------------
# hunger unblock idempotency (D1-12)
# ---------------------------------------------------------------------------


def test_cli_hunger_unblock_already_open_is_idempotent(
    context: CliContext,
) -> None:
    from hungerloop.models.enums import HungerItemStatus
    from hungerloop.models.hunger import HungerItem, HungerLedger

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(
                    id="H-001",
                    title="x",
                    status=HungerItemStatus.OPEN,
                    gap_score=1.0,
                )
            ],
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "unblock", "task-1", "H-001"], obj=context
    )
    assert result.exit_code == 0
    assert "no-op" in result.output


def test_cli_hunger_unblock_all_with_zero_blocked_is_idempotent(
    context: CliContext,
) -> None:
    from hungerloop.models.enums import HungerItemStatus
    from hungerloop.models.hunger import HungerItem, HungerLedger

    repo = context.repo
    repo.create_task("task-1", "Goal")
    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(
                    id="H-001",
                    title="x",
                    status=HungerItemStatus.OPEN,
                    gap_score=1.0,
                )
            ],
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["hunger", "unblock-all", "task-1"], obj=context
    )
    assert result.exit_code == 0
    assert "no BLOCKED items" in result.output
