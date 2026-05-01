"""Workspace inspection commands for the HungerLoop CLI."""
from __future__ import annotations

from pathlib import Path

import click


@click.group()
def workspace() -> None:
    """Inspect task workspaces."""


@workspace.command("best")
@click.argument("task_id")
@click.option("--root", type=click.Path(exists=True), default="workspace")
def workspace_best(task_id: str, root: str) -> None:
    """Show contents of the best workspace."""
    best_dir = Path(root) / "tasks" / task_id / "best" / "files"
    if not best_dir.exists():
        click.echo(f"No best workspace for task {task_id}")
        return
    for p in sorted(best_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(best_dir)
            click.echo(f"  {rel}  ({p.stat().st_size} bytes)")


@workspace.command("candidate")
@click.argument("task_id")
@click.option("--loop", "loop_id", type=int, required=True)
@click.option("--root", type=click.Path(exists=True), default="workspace")
def workspace_candidate(task_id: str, loop_id: int, root: str) -> None:
    """Show contents of a candidate workspace."""
    cand_dir = (
        Path(root) / "tasks" / task_id / "candidates" / f"loop_{loop_id:03d}" / "files"
    )
    if not cand_dir.exists():
        click.echo(f"No candidate workspace for task {task_id} loop {loop_id}")
        return
    for p in sorted(cand_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(cand_dir)
            click.echo(f"  {rel}  ({p.stat().st_size} bytes)")


@workspace.command("rejected")
@click.argument("task_id")
@click.option("--loop", "loop_id", type=int, required=True)
@click.option("--root", type=click.Path(exists=True), default="workspace")
def workspace_rejected(task_id: str, loop_id: int, root: str) -> None:
    """Show contents of a rejected workspace."""
    rej_dir = (
        Path(root) / "tasks" / task_id / "rejected" / f"loop_{loop_id:03d}" / "files"
    )
    if not rej_dir.exists():
        click.echo(f"No rejected workspace for task {task_id} loop {loop_id}")
        return
    for p in sorted(rej_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(rej_dir)
            click.echo(f"  {rel}  ({p.stat().st_size} bytes)")
