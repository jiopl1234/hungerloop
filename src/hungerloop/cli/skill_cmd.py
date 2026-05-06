"""``hungerloop skill`` — SkillCard lifecycle CLI (PRD §18 / §22.3).

v0.5e.1 splits the shipped ``skill list`` command into a candidate +
active surface. The shipped ``skill_cards`` table is no longer the
read source; v0.5e.1 reads from ``skill_card_candidates`` and
``active_skill_cards`` (FR-8).

Commands:

* ``skill list [task_id] [--state <S>]`` — joint listing across
  candidates and actives.
* ``skill show <id>`` — accepts either a candidate id or skill id;
  looks both up.
* ``skill approve <skill_candidate_id>`` — promotes the candidate to
  an :class:`ActiveSkillCard`.
* ``skill reject <skill_candidate_id> --reason <reason>`` — flips
  the candidate to ``rejected``.
* ``skill export <skill_id> --output <path.yaml>`` — write the
  active skill to YAML for cross-machine transport.
* ``skill import <path.yaml>`` — load a YAML export into the local
  ``active_skill_cards`` table with a fresh skill_id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from hungerloop.cli.context import CliContext
from hungerloop.models.events import EventType
from hungerloop.models.skill import ActiveSkillCard

_EXPORT_SCHEMA_VERSION = "1"


@click.group("skill")
def skill() -> None:
    """Skill card lifecycle ops."""


_STATES = ("candidate", "active", "rejected", "deprecated", "all")


@skill.command("list")
@click.argument("task_id", required=False)
@click.option(
    "--state",
    type=click.Choice(list(_STATES)),
    default="all",
    show_default=True,
    help="Filter by lifecycle state.",
)
@click.pass_obj
def skill_list(
    ctx: CliContext,
    task_id: str | None,
    state: str,
) -> None:
    """List skill candidates + active skills.

    The output mixes both lifecycle stages so operators see the full
    picture in one place. Each line carries the row id, lifecycle
    state, source task, accepted-check count, and the derived name.
    """
    candidates = ctx.repo.list_skill_card_candidates(
        task_id=task_id,
        state=state if state in {"candidate", "rejected", "deprecated"} else None,
    )
    if state in {"all", "candidate", "rejected", "deprecated"}:
        rows_candidates = candidates
    else:
        rows_candidates = []

    actives = ctx.repo.list_active_skill_cards(
        state=state if state in {"active", "deprecated"} else None,
    )
    if state in {"all", "active", "deprecated"}:
        rows_actives = actives
    else:
        rows_actives = []

    if not rows_candidates and not rows_actives:
        scope = task_id if task_id is not None else "any task"
        click.echo(f"No skill cards for {scope}.")
        return

    for c in rows_candidates:
        click.echo(
            f"{c.skill_candidate_id} [{c.state}] task={c.task_id} "
            f"checks={len(c.accepted_check_keys)} name={c.name}"
        )
    for a in rows_actives:
        click.echo(
            f"{a.skill_id} [{a.state}] source={a.source_candidate_id} "
            f"checks={len(a.accepted_check_keys)} name={a.name}"
        )


# ---------------------------------------------------------------------------
# skill show
# ---------------------------------------------------------------------------


@skill.command("show")
@click.argument("identifier")
@click.pass_obj
def skill_show(ctx: CliContext, identifier: str) -> None:
    """Show a single candidate or active skill by id."""
    candidate = ctx.repo.get_skill_card_candidate(identifier)
    active = ctx.repo.get_active_skill_card(identifier)
    if candidate is None and active is None:
        raise click.UsageError(
            f"no skill candidate or active skill with id: {identifier}"
        )
    if candidate is not None:
        click.echo("=== SkillCardCandidate ===")
        click.echo(f"skill_candidate_id : {candidate.skill_candidate_id}")
        click.echo(f"task_id            : {candidate.task_id}")
        click.echo(f"state              : {candidate.state}")
        click.echo(f"name               : {candidate.name}")
        click.echo(f"description        : {candidate.description}")
        click.echo(f"trigger_signals    : {candidate.trigger_signals}")
        click.echo(f"preconditions      : {candidate.preconditions}")
        click.echo(f"steps              : {candidate.steps}")
        click.echo(f"tools_used         : {candidate.tools_used}")
        click.echo(f"accepted_check_keys: {candidate.accepted_check_keys}")
        click.echo(f"artifact_ids       : {candidate.artifact_ids}")
        click.echo(f"evidence_ids       : {candidate.evidence_ids}")
        click.echo(f"known_failures     : {candidate.known_failures}")
        click.echo(f"reuse_notes        : {candidate.reuse_notes}")
        click.echo(f"created_at         : {candidate.created_at.isoformat()}")
    if active is not None:
        click.echo("=== ActiveSkillCard ===")
        click.echo(f"skill_id           : {active.skill_id}")
        click.echo(f"source_candidate_id: {active.source_candidate_id}")
        click.echo(f"state              : {active.state}")
        click.echo(f"name               : {active.name}")
        click.echo(f"description        : {active.description}")
        click.echo(f"trigger_signals    : {active.trigger_signals}")
        click.echo(f"preconditions      : {active.preconditions}")
        click.echo(f"steps              : {active.steps}")
        click.echo(f"tools_used         : {active.tools_used}")
        click.echo(f"accepted_check_keys: {active.accepted_check_keys}")
        click.echo(f"artifact_ids       : {active.artifact_ids}")
        click.echo(f"evidence_ids       : {active.evidence_ids}")
        click.echo(f"known_failures     : {active.known_failures}")
        click.echo(f"reuse_notes        : {active.reuse_notes}")
        click.echo(f"activated_at       : {active.activated_at.isoformat()}")
        click.echo(f"activated_by       : {active.activated_by}")


# ---------------------------------------------------------------------------
# skill approve
# ---------------------------------------------------------------------------


@skill.command("approve")
@click.argument("skill_candidate_id")
@click.option(
    "--activated-by",
    "activated_by",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the active row + audit event.",
)
@click.pass_obj
def skill_approve(
    ctx: CliContext,
    skill_candidate_id: str,
    activated_by: str,
) -> None:
    """Promote a SkillCardCandidate to an ActiveSkillCard."""
    cand = ctx.repo.get_skill_card_candidate(skill_candidate_id)
    if cand is None:
        raise click.UsageError(
            f"skill candidate not found: {skill_candidate_id}"
        )
    if cand.state != "candidate":
        click.echo(
            f"Refusing approve: state is {cand.state!r}; only 'candidate' "
            "rows can be approved.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    skill_id = f"SKILL-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    active = ActiveSkillCard(
        skill_id=skill_id,
        source_candidate_id=cand.skill_candidate_id,
        name=cand.name,
        description=cand.description,
        trigger_signals=list(cand.trigger_signals),
        preconditions=list(cand.preconditions),
        steps=list(cand.steps),
        tools_used=list(cand.tools_used),
        accepted_check_keys=list(cand.accepted_check_keys),
        artifact_ids=list(cand.artifact_ids),
        evidence_ids=list(cand.evidence_ids),
        known_failures=list(cand.known_failures),
        reuse_notes=list(cand.reuse_notes),
        state="active",
        activated_at=now,
        activated_by=activated_by,
    )
    updated_candidate = cand.model_copy(update={"state": "active"})

    with ctx.repo.transaction():
        ctx.repo.save_active_skill_card(active)
        ctx.repo.save_skill_card_candidate(updated_candidate)
        ctx.repo.append_event(
            EventType.SKILL_CARD_ACTIVATED,
            {
                "skill_id": skill_id,
                "source_candidate_id": cand.skill_candidate_id,
                "activated_by": activated_by,
            },
            task_id=cand.task_id,
        )
    click.echo(
        f"approved {cand.skill_candidate_id} → {skill_id} "
        f"(activated_by={activated_by})"
    )


# ---------------------------------------------------------------------------
# skill reject
# ---------------------------------------------------------------------------


@skill.command("reject")
@click.argument("skill_candidate_id")
@click.option(
    "--reason",
    required=True,
    help="Rejection reason recorded on the audit event. Required.",
)
@click.option(
    "--reviewer",
    default="unknown",
    show_default=True,
    help="Operator id recorded on the audit event.",
)
@click.pass_obj
def skill_reject(
    ctx: CliContext,
    skill_candidate_id: str,
    reason: str,
    reviewer: str,
) -> None:
    """Reject a candidate; persist the reason on the audit event."""
    cand = ctx.repo.get_skill_card_candidate(skill_candidate_id)
    if cand is None:
        raise click.UsageError(
            f"skill candidate not found: {skill_candidate_id}"
        )
    if cand.state != "candidate":
        click.echo(
            f"Refusing reject: state is {cand.state!r}; only 'candidate' "
            "rows can be rejected.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    updated = cand.model_copy(update={"state": "rejected"})
    with ctx.repo.transaction():
        ctx.repo.save_skill_card_candidate(updated)
        ctx.repo.append_event(
            EventType.SKILL_CARD_REJECTED,
            {
                "skill_candidate_id": cand.skill_candidate_id,
                "reviewer": reviewer,
                "reason": reason,
            },
            task_id=cand.task_id,
        )
    click.echo(
        f"rejected {cand.skill_candidate_id} (reviewer={reviewer}, "
        f"reason={reason!r})"
    )


# ---------------------------------------------------------------------------
# skill export
# ---------------------------------------------------------------------------


@skill.command("export")
@click.argument("skill_id")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination YAML file.",
)
@click.pass_obj
def skill_export(
    ctx: CliContext, skill_id: str, output_path: Path
) -> None:
    """Serialize an :class:`ActiveSkillCard` to YAML."""
    active = ctx.repo.get_active_skill_card(skill_id)
    if active is None:
        raise click.UsageError(f"active skill not found: {skill_id}")

    payload = {
        "schema_version": _EXPORT_SCHEMA_VERSION,
        "skill_id": active.skill_id,
        "source_candidate_id": active.source_candidate_id,
        "name": active.name,
        "description": active.description,
        "trigger_signals": list(active.trigger_signals),
        "preconditions": list(active.preconditions),
        "steps": list(active.steps),
        "tools_used": list(active.tools_used),
        "accepted_check_keys": list(active.accepted_check_keys),
        "artifact_ids": list(active.artifact_ids),
        "evidence_ids": list(active.evidence_ids),
        "known_failures": list(active.known_failures),
        "reuse_notes": list(active.reuse_notes),
        "activated_at": active.activated_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "activated_by": active.activated_by,
    }
    output_path.write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )
    click.echo(f"exported {skill_id} → {output_path}")


# ---------------------------------------------------------------------------
# skill import
# ---------------------------------------------------------------------------


@skill.command("import")
@click.argument(
    "yaml_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.pass_obj
def skill_import(ctx: CliContext, yaml_path: Path) -> None:
    """Load a YAML export into ``active_skill_cards`` with a fresh id."""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        click.echo(
            "Refusing import: YAML root must be a mapping.", err=True
        )
        raise click.exceptions.Exit(2)
    schema_version = raw.get("schema_version")
    if schema_version != _EXPORT_SCHEMA_VERSION:
        click.echo(
            f"Refusing import: Unsupported schema_version: {schema_version!r}",
            err=True,
        )
        raise click.exceptions.Exit(2)

    new_skill_id = f"SKILL-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload: dict[str, object] = {
        "skill_id": new_skill_id,
        "source_candidate_id": f"IMPORTED-{uuid.uuid4().hex[:8]}",
        "name": raw.get("name", ""),
        "description": raw.get("description", ""),
        "trigger_signals": raw.get("trigger_signals", []),
        "preconditions": raw.get("preconditions", []),
        "steps": raw.get("steps", []),
        "tools_used": raw.get("tools_used", []),
        "accepted_check_keys": raw.get("accepted_check_keys", []),
        "artifact_ids": raw.get("artifact_ids", []),
        "evidence_ids": raw.get("evidence_ids", []),
        "known_failures": raw.get("known_failures", []),
        "reuse_notes": raw.get("reuse_notes", []),
        "state": "active",
        "activated_at": now,
        "activated_by": f"<imported_from {yaml_path.name}>",
    }
    try:
        active = ActiveSkillCard.model_validate(payload)
    except ValidationError as exc:
        click.echo(
            f"Refusing import: invalid ActiveSkillCard payload: {exc}",
            err=True,
        )
        raise click.exceptions.Exit(2) from exc

    with ctx.repo.transaction():
        ctx.repo.save_active_skill_card(active)
        ctx.repo.append_event(
            EventType.SKILL_CARD_IMPORTED,
            {
                "skill_id": new_skill_id,
                "source_path": str(yaml_path),
                "original_skill_id": raw.get("skill_id"),
            },
            task_id=None,
        )
    click.echo(f"imported {yaml_path.name} as {new_skill_id}")
