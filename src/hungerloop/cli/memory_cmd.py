"""``hungerloop memory`` — MemoryCandidate lifecycle CLI (PRD §19 / §22.3).

Five operator verbs land in v0.5e.0:

* ``memory approve <candidate_id>`` — flip ``state="approved"``,
  insert a :class:`PromotedMemory` row, emit
  ``memory_candidate_approved`` + ``memory_promoted``.
* ``memory reject <candidate_id> --reason <reason>`` — flip
  ``state="rejected"``, persist ``rejection_reason``, emit
  ``memory_candidate_rejected``.
* ``memory defer <candidate_id>`` — idempotent flip to
  ``state="deferred"``; emits ``memory_candidate_deferred`` only on
  state-changing transitions.
* ``memory expire <candidate_id>`` — operator-driven manual expiry;
  the v0.6 auto-expiry sweep will reuse this path.
* ``memory list <task_id>`` (extended) — adds ``--state`` filter
  for ``"deferred"`` and ``--all-tasks`` fanout.
* ``memory promoted list / show`` — read-only views over the
  ``promoted_memories`` table.

Approval validation (FR-14) is side-effect-free up to and including
the ``--force``/predicate gate. Mutations execute inside
``repo.transaction()`` so a crash mid-promotion leaves either both
the candidate state update *and* the PromotedMemory insert applied,
or neither (NFR-5).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import click

from hungerloop.cli.context import CliContext
from hungerloop.models.events import EventType
from hungerloop.models.memory import MemoryCandidate, PromotedMemory

_STATES = (
    "proposed",
    "approved",
    "rejected",
    "expired",
    "superseded",
    "deferred",
)


@click.group("memory")
def memory() -> None:
    """Memory candidate inspection + lifecycle ops."""


# ---------------------------------------------------------------------------
# memory list (extended)
# ---------------------------------------------------------------------------


@memory.command("list")
@click.argument("task_id", required=False)
@click.option(
    "--state",
    type=click.Choice([*_STATES, "all"]),
    default="all",
    show_default=True,
    help=(
        "Filter by lifecycle state (PRD §19.1). v0.5e.0 adds 'deferred'."
    ),
)
@click.option(
    "--all-tasks",
    is_flag=True,
    default=False,
    help=(
        "List candidates across every task. ``task_id`` becomes optional; "
        "any explicit value is ignored."
    ),
)
@click.pass_obj
def memory_list(
    ctx: CliContext,
    task_id: str | None,
    state: str,
    all_tasks: bool,
) -> None:
    """Print each :class:`MemoryCandidate` with predicate flags."""
    if not all_tasks and not task_id:
        raise click.UsageError(
            "task_id is required unless --all-tasks is passed."
        )

    if all_tasks:
        # InMemoryRepository / SQLite both expose list_memory_candidates
        # per-task; iterate across every known task.
        candidates: list[MemoryCandidate] = []
        for cand_task in _iter_known_task_ids(ctx):
            candidates.extend(ctx.repo.list_memory_candidates(cand_task))
    else:
        assert task_id is not None
        candidates = ctx.repo.list_memory_candidates(task_id)

    if state != "all":
        candidates = [c for c in candidates if c.state == state]
    if not candidates:
        target = "any task" if all_tasks else task_id
        click.echo(f"No memory candidates for {target}.")
        return
    for c in candidates:
        flags = "".join(
            "1" if getattr(c, name) else "0"
            for name in ("action_verified", "reusable", "non_volatile", "traceable")
        )
        click.echo(
            f"{c.candidate_id} [{c.task_id}] [{c.state}] [{flags}] "
            f"{c.memory_type}: {c.content}"
        )


# ---------------------------------------------------------------------------
# memory approve / reject / defer / expire
# ---------------------------------------------------------------------------


@memory.command("approve")
@click.argument("candidate_id")
@click.option(
    "--reviewer",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the candidate row + audit event.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Bypass the reusable-only gate (FR-14 step 5).",
)
@click.pass_obj
def memory_approve(
    ctx: CliContext,
    candidate_id: str,
    reviewer: str,
    force: bool,
) -> None:
    """Approve a candidate and persist a :class:`PromotedMemory`."""
    cand = ctx.repo.get_memory_candidate(candidate_id)
    if cand is None:
        raise click.UsageError(f"memory candidate not found: {candidate_id}")

    # FR-14 steps 1-5: validate side-effect-free.
    if cand.state not in {"proposed", "deferred"}:
        click.echo(
            f"Refusing approve: state is {cand.state!r}; only "
            "'proposed' or 'deferred' rows can be approved.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    if not cand.evidence_ids:
        click.echo(
            "Refusing approve: candidate has no evidence_ids — promote a "
            "row that survived validation.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    if not cand.action_verified:
        click.echo(
            "Refusing approve: action_verified is False; the candidate's "
            "evidence does not overlap best.evidence_ids. Re-run "
            "validation against the source loop.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    if not cand.traceable:
        click.echo(
            "Refusing approve: traceable is False; some candidate evidence "
            "is missing from best.evidence_ids. Inspect the source loop's "
            "validation report.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    if not cand.reusable and not force:
        click.echo(
            "Refusing approve: reusable is False (content contains "
            "task-specific tokens). Pass --force to override.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    # FR-14 step 6+7+8: mutations + audit, all inside one transaction.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    updated = cand.model_copy(
        update={
            "state": "approved",
            "status": "approved",
            "reviewer": reviewer,
            "reviewed_at": now,
        }
    )
    memory_id = f"prom-{uuid.uuid4().hex[:8]}"
    promoted = PromotedMemory(
        memory_id=memory_id,
        source_candidate_id=cand.candidate_id,
        task_id=cand.task_id,
        content=cand.content,
        memory_type=cand.memory_type,
        layer="task",
        evidence_ids=list(cand.evidence_ids),
        accepted_check_keys=list(cand.accepted_check_keys),
        confidence=cand.confidence,
        created_at=now,
        approved_by=reviewer,
    )

    with ctx.repo.transaction():
        ctx.repo.save_memory_candidate(updated)
        ctx.repo.save_promoted_memory(promoted)
        ctx.repo.append_event(
            EventType.MEMORY_CANDIDATE_APPROVED,
            {
                "candidate_id": cand.candidate_id,
                "reviewer": reviewer,
                "memory_id": memory_id,
            },
            task_id=cand.task_id,
        )
        ctx.repo.append_event(
            EventType.MEMORY_PROMOTED,
            {
                "memory_id": memory_id,
                "source_candidate_id": cand.candidate_id,
                "approved_by": reviewer,
            },
            task_id=cand.task_id,
        )

    click.echo(
        f"approved {cand.candidate_id} on {cand.task_id} "
        f"(promoted as {memory_id}, reviewer={reviewer})"
    )


@memory.command("reject")
@click.argument("candidate_id")
@click.option(
    "--reviewer",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the candidate row + audit event.",
)
@click.option(
    "--reason",
    required=True,
    help=(
        "Rejection reason recorded on the candidate row. Required; "
        "missing it is a usage error."
    ),
)
@click.pass_obj
def memory_reject(
    ctx: CliContext,
    candidate_id: str,
    reviewer: str,
    reason: str,
) -> None:
    """Reject a candidate; persist ``rejection_reason``."""
    cand = ctx.repo.get_memory_candidate(candidate_id)
    if cand is None:
        raise click.UsageError(f"memory candidate not found: {candidate_id}")

    if cand.state not in {"proposed", "deferred"}:
        click.echo(
            f"Refusing reject: state is {cand.state!r}; only "
            "'proposed' or 'deferred' rows can be rejected.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    updated = cand.model_copy(
        update={
            "state": "rejected",
            "status": "rejected",
            "reviewer": reviewer,
            "reviewed_at": now,
            "rejection_reason": reason,
        }
    )
    with ctx.repo.transaction():
        ctx.repo.save_memory_candidate(updated)
        ctx.repo.append_event(
            EventType.MEMORY_CANDIDATE_REJECTED,
            {
                "candidate_id": cand.candidate_id,
                "reviewer": reviewer,
                "reason": reason,
            },
            task_id=cand.task_id,
        )
    click.echo(
        f"rejected {cand.candidate_id} on {cand.task_id} "
        f"(reviewer={reviewer}, reason={reason!r})"
    )


@memory.command("defer")
@click.argument("candidate_id")
@click.option(
    "--reviewer",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the audit event.",
)
@click.pass_obj
def memory_defer(
    ctx: CliContext,
    candidate_id: str,
    reviewer: str,
) -> None:
    """Defer a candidate; idempotent on already-deferred rows."""
    cand = ctx.repo.get_memory_candidate(candidate_id)
    if cand is None:
        raise click.UsageError(f"memory candidate not found: {candidate_id}")

    if cand.state == "deferred":
        click.echo(
            f"{cand.candidate_id} is already deferred — no-op (idempotent)."
        )
        return
    if cand.state not in {"proposed"}:
        click.echo(
            f"Refusing defer: state is {cand.state!r}; only 'proposed' "
            "rows can be deferred.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    updated = cand.model_copy(
        update={
            "state": "deferred",
            "reviewer": reviewer,
            "reviewed_at": now,
        }
    )
    with ctx.repo.transaction():
        ctx.repo.save_memory_candidate(updated)
        ctx.repo.append_event(
            EventType.MEMORY_CANDIDATE_DEFERRED,
            {
                "candidate_id": cand.candidate_id,
                "reviewer": reviewer,
            },
            task_id=cand.task_id,
        )
    click.echo(
        f"deferred {cand.candidate_id} on {cand.task_id} "
        f"(reviewer={reviewer})"
    )


@memory.command("expire")
@click.argument("candidate_id")
@click.option(
    "--reviewer",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the audit event.",
)
@click.pass_obj
def memory_expire(
    ctx: CliContext,
    candidate_id: str,
    reviewer: str,
) -> None:
    """Manually expire a candidate (v0.6 auto-sweep reuses this path)."""
    cand = ctx.repo.get_memory_candidate(candidate_id)
    if cand is None:
        raise click.UsageError(f"memory candidate not found: {candidate_id}")

    if cand.state == "expired":
        click.echo(
            f"{cand.candidate_id} is already expired — no-op (idempotent)."
        )
        return

    now = datetime.now(timezone.utc).replace(microsecond=0)
    updated = cand.model_copy(
        update={
            "state": "expired",
            "reviewer": reviewer,
            "reviewed_at": now,
        }
    )
    with ctx.repo.transaction():
        ctx.repo.save_memory_candidate(updated)
        ctx.repo.append_event(
            EventType.MEMORY_CANDIDATE_EXPIRED,
            {
                "candidate_id": cand.candidate_id,
                "reviewer": reviewer,
            },
            task_id=cand.task_id,
        )
    click.echo(
        f"expired {cand.candidate_id} on {cand.task_id} "
        f"(reviewer={reviewer})"
    )


# ---------------------------------------------------------------------------
# memory promoted (list / show)
# ---------------------------------------------------------------------------


@memory.group("promoted")
def memory_promoted() -> None:
    """Read-only views over PromotedMemory rows."""


@memory_promoted.command("list")
@click.argument("task_id", required=False)
@click.pass_obj
def memory_promoted_list(ctx: CliContext, task_id: str | None) -> None:
    rows = ctx.repo.list_promoted_memories(task_id)
    if not rows:
        scope = task_id if task_id else "any task"
        click.echo(f"No promoted memories for {scope}.")
        return
    for m in rows:
        click.echo(
            f"{m.memory_id} [{m.task_id}] [{m.layer}] {m.memory_type}: "
            f"{m.content}"
        )


@memory_promoted.command("show")
@click.argument("memory_id")
@click.pass_obj
def memory_promoted_show(ctx: CliContext, memory_id: str) -> None:
    m = ctx.repo.get_promoted_memory(memory_id)
    if m is None:
        raise click.UsageError(f"promoted memory not found: {memory_id}")
    click.echo(f"memory_id          : {m.memory_id}")
    click.echo(f"source_candidate_id: {m.source_candidate_id}")
    click.echo(f"task_id            : {m.task_id}")
    click.echo(f"memory_type        : {m.memory_type}")
    click.echo(f"layer              : {m.layer}")
    click.echo(f"confidence         : {m.confidence:.4f}")
    click.echo(f"created_at         : {m.created_at.isoformat()}")
    click.echo(f"approved_by        : {m.approved_by}")
    click.echo(f"evidence_ids       : {m.evidence_ids}")
    click.echo(f"accepted_check_keys: {m.accepted_check_keys}")
    click.echo(f"reuse_scenarios    : {m.reuse_scenarios}")
    click.echo(f"content            : {m.content}")


# ---------------------------------------------------------------------------
# Internal — task-id discovery for --all-tasks
# ---------------------------------------------------------------------------


def _iter_known_task_ids(ctx: CliContext) -> list[str]:
    """Public enumeration of known task ids via the repository protocol."""
    return ctx.repo.list_task_ids()
