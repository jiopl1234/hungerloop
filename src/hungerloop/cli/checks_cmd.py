"""Checks status command for the HungerLoop CLI."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import click


@click.command()
@click.argument("task_id")
@click.option("--db", type=click.Path(), default=None, help="Path to blackboard.sqlite")
def checks(task_id: str, db: str | None) -> None:
    """Show accepted check status for a task."""
    if db is None:
        db_path = Path("workspace") / "tasks" / task_id / "blackboard.sqlite"
    else:
        db_path = Path(db)

    if not db_path.exists():
        click.echo(f"No database found at {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT check_key, hunger_item_id, check_index, accepted_at_loop, validation_id "
            "FROM accepted_checks WHERE task_id = ? ORDER BY check_key",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        click.echo("accepted_checks table not found")
        return
    finally:
        conn.close()

    if not rows:
        click.echo("No accepted checks.")
        return

    for check_key, _item_id, _idx, loop, val_id in rows:
        click.echo(f"{check_key}  PASS  loop={loop}  {val_id}")
