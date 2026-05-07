"""``hungerloop memory`` CLI tests (PRD §19 / E0-07..12).

Covers:

* approve happy path + every refusal branch (FR-14 steps 1-5)
* --force override for non-reusable rows
* reject mandatory --reason + persistence
* defer idempotency
* expire idempotency + state transition
* promoted list / show + memory_promoted event row
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.memory import MemoryCandidate
from hungerloop.repository.in_memory_repo import InMemoryRepository


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


def _seed_candidate(
    repo: InMemoryRepository,
    *,
    candidate_id: str = "cand-1",
    content: str = "Verified acceptance check H-001:0",
    evidence_ids: list[str] | None = None,
    action_verified: bool = True,
    traceable: bool = True,
    reusable: bool = True,
    state: str = "proposed",
) -> MemoryCandidate:
    repo.create_task("t1", "Goal")
    cand = MemoryCandidate(
        candidate_id=candidate_id,
        task_id="t1",
        content=content,
        evidence_ids=evidence_ids if evidence_ids is not None else ["ev-1"],
        accepted_check_keys=["H-001:0"],
        action_verified=action_verified,
        traceable=traceable,
        reusable=reusable,
        state=state,  # type: ignore[arg-type]
    )
    repo.save_memory_candidate(cand)
    return cand


# ---------------------------------------------------------------------------
# memory approve
# ---------------------------------------------------------------------------


def test_approve_happy_path_promotes_and_emits_events(
    context: CliContext,
) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "approve", "cand-1", "--reviewer", "alice"],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    assert "approved cand-1" in result.output

    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.state == "approved"
    assert cand.reviewer == "alice"
    assert cand.reviewed_at is not None

    promoted = context.repo.list_promoted_memories("t1")
    assert len(promoted) == 1
    assert promoted[0].source_candidate_id == "cand-1"
    assert promoted[0].approved_by == "alice"

    types = {ev["event_type"] for ev in context.repo.list_events("t1")}
    assert "memory_candidate_approved" in types
    assert "memory_promoted" in types


def test_approve_unknown_candidate_usage_error(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["memory", "approve", "missing"], obj=context
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_approve_refuses_non_proposed_state(context: CliContext) -> None:
    _seed_candidate(context.repo, state="approved")
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "state is" in result.output


def test_approve_refuses_no_evidence(context: CliContext) -> None:
    _seed_candidate(context.repo, evidence_ids=[])
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "evidence_ids" in result.output


def test_approve_refuses_action_not_verified(context: CliContext) -> None:
    _seed_candidate(context.repo, action_verified=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "action_verified" in result.output


def test_approve_refuses_not_traceable(context: CliContext) -> None:
    _seed_candidate(context.repo, traceable=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "traceable" in result.output


def test_approve_refuses_non_reusable_without_force(context: CliContext) -> None:
    _seed_candidate(context.repo, reusable=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "reusable" in result.output


def test_approve_force_overrides_reusable_gate(context: CliContext) -> None:
    _seed_candidate(context.repo, reusable=False)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "approve", "cand-1", "--force"],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None and cand.state == "approved"


# ---------------------------------------------------------------------------
# memory reject
# ---------------------------------------------------------------------------


def test_reject_persists_reason_and_emits_event(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "reject", "cand-1", "--reason", "irrelevant"],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.state == "rejected"
    assert cand.rejection_reason == "irrelevant"
    types = {ev["event_type"] for ev in context.repo.list_events("t1")}
    assert "memory_candidate_rejected" in types


def test_reject_missing_reason_is_usage_error(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "reject", "cand-1"], obj=context)
    assert result.exit_code == 2
    assert "--reason" in result.output


# ---------------------------------------------------------------------------
# memory defer
# ---------------------------------------------------------------------------


def test_defer_then_approve_round_trips(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "defer", "cand-1"], obj=context)
    assert result.exit_code == 0, result.output
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None and cand.state == "deferred"

    # Approve from deferred (FR-14 step 1 allows deferred → approved).
    result = runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    assert result.exit_code == 0, result.output
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None and cand.state == "approved"


def test_defer_idempotent_on_already_deferred(context: CliContext) -> None:
    _seed_candidate(context.repo, state="deferred")
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "defer", "cand-1"], obj=context)
    assert result.exit_code == 0
    assert "no-op" in result.output


# ---------------------------------------------------------------------------
# memory expire
# ---------------------------------------------------------------------------


def test_expire_state_transition(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "expire", "cand-1"], obj=context)
    assert result.exit_code == 0, result.output
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None and cand.state == "expired"


def test_expire_idempotent(context: CliContext) -> None:
    _seed_candidate(context.repo, state="expired")
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "expire", "cand-1"], obj=context)
    assert result.exit_code == 0
    assert "no-op" in result.output


# ---------------------------------------------------------------------------
# memory list extensions
# ---------------------------------------------------------------------------


def test_list_state_filter_includes_deferred(context: CliContext) -> None:
    _seed_candidate(context.repo, candidate_id="cand-1", state="proposed")
    _seed_candidate(context.repo, candidate_id="cand-2", state="deferred")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "list", "t1", "--state", "deferred"],
        obj=context,
    )
    assert result.exit_code == 0
    assert "cand-2" in result.output
    assert "cand-1" not in result.output


def test_list_all_tasks_fanout(context: CliContext) -> None:
    repo = context.repo
    repo.create_task("t1", "G1")
    repo.create_task("t2", "G2")
    repo.save_memory_candidate(
        MemoryCandidate(candidate_id="m-1", task_id="t1", content="a")
    )
    repo.save_memory_candidate(
        MemoryCandidate(candidate_id="m-2", task_id="t2", content="b")
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "list", "--all-tasks"],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    assert "m-1" in result.output
    assert "m-2" in result.output


def test_show_prints_candidate_details(context: CliContext) -> None:
    _seed_candidate(
        context.repo,
        traceable=False,
        reusable=False,
        action_verified=False,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "show", "cand-1"], obj=context)
    assert result.exit_code == 0, result.output
    assert "candidate_id" in result.output
    assert "cand-1" in result.output
    assert "action_verified     : False" in result.output
    assert "reusable            : False" in result.output
    assert "traceable           : False" in result.output
    assert "accepted_check_keys : ['H-001:0']" in result.output


# ---------------------------------------------------------------------------
# memory promoted (list / show)
# ---------------------------------------------------------------------------


def test_promoted_list_empty(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "promoted", "list", "t1"], obj=context)
    assert result.exit_code == 0
    assert "No promoted memories" in result.output


def test_promoted_show_after_approve(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    promoted = context.repo.list_promoted_memories("t1")
    assert promoted
    memory_id = promoted[0].memory_id

    show = runner.invoke(
        cli, ["memory", "promoted", "show", memory_id], obj=context
    )
    assert show.exit_code == 0
    assert memory_id in show.output
    assert "cand-1" in show.output  # source_candidate_id


def test_approval_now_uses_utc_seconds_resolution(context: CliContext) -> None:
    """Pin: reviewed_at is UTC second-resolution so SQLite TEXT and
    InMemory string serialization stay deterministic."""
    _seed_candidate(context.repo)
    runner = CliRunner()
    runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None
    assert cand.reviewed_at is not None
    assert cand.reviewed_at.tzinfo == timezone.utc
    # Microseconds stripped — matches seconds resolution chosen across the
    # rest of the wire schema (events.created_at, stop_reports.created_at).
    assert cand.reviewed_at == cand.reviewed_at.replace(microsecond=0)


def test_approve_sets_reviewed_at_close_to_now(context: CliContext) -> None:
    _seed_candidate(context.repo)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    runner = CliRunner()
    runner.invoke(cli, ["memory", "approve", "cand-1"], obj=context)
    cand = context.repo.get_memory_candidate("cand-1")
    assert cand is not None and cand.reviewed_at is not None
    delta = (cand.reviewed_at - before).total_seconds()
    assert -2 <= delta <= 5
