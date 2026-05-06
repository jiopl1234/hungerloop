"""Unit tests for SkillManager + memory/skill list CLI commands (PRD §20)."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import StopReason
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.tracing import StopReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.skill_manager import SkillManager


def _stop(reason: StopReason, status: str = "completed") -> StopReport:
    return StopReport(
        task_id="t1",
        stop_reason=reason,
        goal_status=status,  # type: ignore[arg-type]
    )


def _best(checks: list[str]) -> BestState:
    return BestState(
        task_id="t1",
        state_id="best-1",
        summary="ok",
        accepted_check_keys=checks,
        evidence_ids=["ev-1", "ev-2"],
        artifact_ids=["art-1"],
    )


# ---- SkillManager trigger -------------------------------------------------


def test_skill_card_only_when_done() -> None:
    repo = InMemoryRepository()
    repo.save_best_state(_best(["H-001:0", "H-001:1"]))
    mgr = SkillManager(repo)
    assert mgr.maybe_create_skill_card("t1", _stop(StopReason.SAFETY_STOP, "abandoned")) is None
    assert mgr.maybe_create_skill_card("t1", _stop(StopReason.HUNGER_EXPIRED, "abandoned")) is None


def test_skill_card_requires_two_accepted_checks() -> None:
    repo = InMemoryRepository()
    repo.save_best_state(_best(["H-001:0"]))
    mgr = SkillManager(repo)
    assert mgr.maybe_create_skill_card("t1", _stop(StopReason.DONE)) is None


def test_skill_card_none_when_best_is_missing() -> None:
    repo = InMemoryRepository()
    mgr = SkillManager(repo)
    assert mgr.maybe_create_skill_card("t1", _stop(StopReason.DONE)) is None


def test_skill_card_generated_on_done_with_two_checks() -> None:
    """v0.5e.1 trigger: writes a SkillCardCandidate (not a SkillCard).

    The legacy ``skill_cards`` table is intentionally untouched —
    operators promote the candidate via ``hungerloop skill approve``.
    """
    from hungerloop.models.tracing import LoopTrace

    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    repo.save_best_state(_best(["H-001:0", "H-001:1"]))
    # Trigger condition #4: a successful tool_call evidence row.
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="write_file",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    # Trigger condition #5: a committed LoopTrace with no regressions.
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=1,
            phase="exploit",
            active_hunger=10.0,
            drive_budget=10.0,
            work_pressure=0.0,
            committed=True,
            delta_summary="wrote report.md",
        )
    )

    mgr = SkillManager(repo)
    candidate = mgr.maybe_create_skill_card("t1", _stop(StopReason.DONE))
    assert candidate is not None
    assert candidate.task_id == "t1"
    assert candidate.accepted_check_keys == ["H-001:0", "H-001:1"]
    assert candidate.evidence_ids == ["ev-1", "ev-2"]
    assert candidate.state == "candidate"

    saved = repo.list_skill_card_candidates(task_id="t1")
    assert len(saved) == 1
    assert saved[0].skill_candidate_id == candidate.skill_candidate_id

    # Legacy skill_cards table is intentionally empty.
    assert repo.list_skill_cards("t1") == []

    # Audit event fired.
    types = {ev["event_type"] for ev in repo.list_events("t1")}
    assert "skill_card_candidate_created" in types


# ---- CLI list commands ----------------------------------------------------


@pytest.fixture
def populated_context(tmp_path: Path) -> CliContext:
    from hungerloop.models.tracing import LoopTrace

    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    repo.save_memory_candidate(
        MemoryCandidate(
            candidate_id="mem-1",
            task_id="t1",
            content="Verified acceptance check H-001:0",
            evidence_ids=["ev-1"],
            action_verified=True,
            reusable=True,
            non_volatile=False,
            traceable=True,
        )
    )
    repo.save_best_state(_best(["H-001:0", "H-001:1"]))
    repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="write_file",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=1,
            phase="exploit",
            active_hunger=10.0,
            drive_budget=10.0,
            work_pressure=0.0,
            committed=True,
            delta_summary="wrote report.md",
        )
    )
    SkillManager(repo).maybe_create_skill_card("t1", _stop(StopReason.DONE))
    return CliContext(repo=repo, workspace_root=tmp_path)


def test_memory_list_prints_candidates(populated_context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "list", "t1"], obj=populated_context)
    assert result.exit_code == 0, result.output
    assert "mem-1" in result.output
    # action_verified=1, reusable=1, non_volatile=0, traceable=1
    assert "[1101]" in result.output


def test_memory_list_handles_empty(tmp_path: Path) -> None:
    ctx = CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "list", "t1"], obj=ctx)
    assert result.exit_code == 0
    assert "No memory candidates" in result.output


def test_skill_list_prints_cards(populated_context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "list", "t1"], obj=populated_context)
    assert result.exit_code == 0, result.output
    assert "task=t1" in result.output
    assert "checks=2" in result.output


def test_skill_list_no_args_lists_all_tasks(populated_context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "list"], obj=populated_context)
    assert result.exit_code == 0, result.output
    assert "task=t1" in result.output
