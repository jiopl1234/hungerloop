"""Unit tests for ToolHarness (PRD §9 + §28.4 + §28.5 + §28.11)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import EvidenceType, LoopPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.budget_guard import BudgetGuard, WorkerBudgetExceeded
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.tool_harness import (
    ToolHarness,
    ToolNotPermitted,
)
from hungerloop.services.tools import default_tool_registry


def _ctx(
    *,
    allow_shell: bool = True,
    allow_file_write: bool = True,
    allow_network: bool = False,
    max_tool_calls: int = 5,
) -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="m",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="cand",
        budget=BudgetAllocation(
            phase=LoopPhase.EXPLORE,
            allow_shell=allow_shell,
            allow_file_write=allow_file_write,
            allow_network=allow_network,
            max_tool_calls=max_tool_calls,
        ),
    )


@pytest.fixture
def harness_setup(tmp_path: Path) -> tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path]:
    repo = InMemoryRepository()
    guard = BudgetGuard()
    sandbox = SandboxRunner(repo)
    harness = ToolHarness(repo, default_tool_registry(sandbox), guard)
    return harness, repo, guard, tmp_path


async def test_unknown_tool_returns_error_result(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, repo, _, workspace = harness_setup
    result = await harness.execute(_ctx(), "missing_tool", {}, workspace)
    assert result.success is False
    assert result.error_type == "unknown_tool"
    assert len(result.evidence_ids) == 1
    evidence = repo._evidence[result.evidence_ids[0]]
    assert evidence["type"] == EvidenceType.TOOL_CALL.value
    assert evidence["success"] is False
    assert repo.get_usage_snapshot("t1").tool_calls == 1


async def test_write_file_records_evidence_and_artifact(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, repo, _, workspace = harness_setup
    result = await harness.execute(
        _ctx(),
        "write_file",
        {"path": "report.md", "content": "# demo\n"},
        workspace,
    )
    assert result.success is True
    assert len(result.evidence_ids) == 1
    assert len(result.artifact_ids) == 1
    evidence = repo._evidence[result.evidence_ids[0]]
    assert evidence["type"] == EvidenceType.TOOL_CALL.value
    assert evidence["tool_name"] == "write_file"
    artifact = repo._artifacts[result.artifact_ids[0]]
    assert artifact.artifact_type == "file_write"
    assert artifact.path == "report.md"


async def test_failed_tool_still_writes_evidence_no_artifact(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, repo, _, workspace = harness_setup
    result = await harness.execute(
        _ctx(),
        "patch_file",
        {"path": "missing.py", "old_text": "x", "new_text": "y"},
        workspace,
    )
    assert result.success is False
    assert len(result.evidence_ids) == 1  # tool_call evidence still recorded
    assert result.artifact_ids == []
    assert repo._evidence[result.evidence_ids[0]]["success"] is False


async def test_run_shell_attaches_sandbox_evidence(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, repo, _, workspace = harness_setup
    result = await harness.execute(
        _ctx(),
        "run_shell",
        {"argv": ["echo", "ok"], "timeout": 5},
        workspace,
    )
    assert result.success is True
    # Two evidence rows: tool_call envelope + sandbox shell_output.
    assert len(result.evidence_ids) == 2
    types = {repo._evidence[eid]["type"] for eid in result.evidence_ids}
    assert types == {
        EvidenceType.TOOL_CALL.value,
        EvidenceType.SANDBOX_RUN.value,
    }


async def test_shell_blocked_when_allow_shell_false(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, _, _, workspace = harness_setup
    with pytest.raises(ToolNotPermitted, match="shell disabled"):
        await harness.execute(
            _ctx(allow_shell=False),
            "run_shell",
            {"argv": ["echo", "x"]},
            workspace,
        )


async def test_file_write_blocked_when_allow_file_write_false(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, _, _, workspace = harness_setup
    with pytest.raises(ToolNotPermitted, match="file_write disabled"):
        await harness.execute(
            _ctx(allow_file_write=False),
            "write_file",
            {"path": "x.txt", "content": "x"},
            workspace,
        )


async def test_policy_denial_emits_terminal_tool_call_failed_event(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    """Post-review I5: every TOOL_CALL_STARTED has a terminal twin.

    Synchronous policy denials (``ToolNotPermitted``) used to leave a
    STARTED event with no SUCCEEDED/FAILED counterpart, breaking
    audit-aggregation invariants.
    """
    harness, repo, _, workspace = harness_setup
    with pytest.raises(ToolNotPermitted):
        await harness.execute(
            _ctx(allow_shell=False),
            "run_shell",
            {"argv": ["echo", "x"]},
            workspace,
        )

    types = [
        ev["event_type"] for ev in repo.list_events("t1")
        if ev.get("event_type", "").startswith("tool_call_")
    ]
    assert types == ["tool_call_started", "tool_call_failed"]
    failed_payload = next(
        ev["payload"]
        for ev in repo.list_events("t1")
        if ev["event_type"] == "tool_call_failed"
    )
    assert failed_payload["error_type"] == "not_permitted"


async def test_budget_guard_pre_check_blocks_overflow(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    """Pre-call assert_can_spend rejects when current+addl exceeds max."""
    harness, repo, guard, workspace = harness_setup
    ctx = _ctx(max_tool_calls=1)
    guard.record(ctx.task_id, ctx.loop_id, ctx.agent_id, tool_calls=1)
    with pytest.raises(WorkerBudgetExceeded, match="tool_call"):
        await harness.execute(
            ctx, "write_file", {"path": "x.txt", "content": "x"}, workspace
        )
    types = [
        ev["event_type"]
        for ev in repo.list_events("t1")
        if ev.get("event_type", "").startswith("tool_call_")
    ]
    assert types == ["tool_call_started", "tool_call_failed"]
    failed_payload = next(
        ev["payload"]
        for ev in repo.list_events("t1")
        if ev["event_type"] == "tool_call_failed"
    )
    assert failed_payload["error_type"] == "budget_exceeded"


async def test_budget_guard_records_after_call(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    harness, _, guard, workspace = harness_setup
    ctx = _ctx()
    await harness.execute(
        ctx, "write_file", {"path": "x.txt", "content": "x"}, workspace
    )
    snap = guard.usage_for(ctx.task_id, ctx.loop_id, ctx.agent_id)
    assert snap.tool_calls == 1
    assert snap.elapsed_seconds >= 0.0


async def test_path_safety_violation_surfaces_as_invalid_args(
    harness_setup: tuple[ToolHarness, InMemoryRepository, BudgetGuard, Path],
) -> None:
    """Path-escape attempts surface as invalid_args ToolResult + evidence row.

    Tools no longer crash the worker on bad input; the harness catches
    PermissionError / ValueError, writes a tool_call evidence row, and
    returns a structured ToolResult so the worker keeps making progress
    on its other actions.
    """
    harness, repo, _, workspace = harness_setup
    result = await harness.execute(
        _ctx(),
        "write_file",
        {"path": "../escape.txt", "content": "x"},
        workspace,
    )
    assert result.success is False
    assert result.error_type == "invalid_args"
    assert len(result.evidence_ids) == 1
    evidence = repo._evidence[result.evidence_ids[0]]
    assert evidence["type"] == "tool_call"
    assert evidence["success"] is False
