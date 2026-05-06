"""``hungerloop skill`` — SkillCard lifecycle CLI (PRD §18 / §22.3).

v0.5e.1 splits the shipped ``skill list`` command into a candidate +
active surface. The shipped ``skill_cards`` table is no longer the
read source; v0.5e.1 reads from ``skill_card_candidates`` and
``active_skill_cards`` (FR-8).

Commands:

* ``skill list [task_id] [--state <S>]`` — joint listing across
  candidates and actives. ``--state`` filters by lifecycle state
  (``candidate`` / ``active`` / ``rejected`` / ``deprecated`` /
  ``all``); default ``all``. Without ``task_id``, lists across every
  task.

E1-08 onward will add ``show`` / ``approve`` / ``reject`` / ``export`` /
``import``.
"""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext


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
