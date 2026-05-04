"""``hungerloop trace`` command group (PRD §22.8).

Exposes the canonical event projection as JSONL on stdout. Downstream
shippers (Prometheus exporters, log forwarders, ad-hoc ``jq``
pipelines) consume one line per event with the schema documented in
PRD §22.8:

    {"task_id":"t1","loop_id":3,"event_type":"loop_committed",
     "payload":{...},"created_at":"2026-05-04T13:22:09Z"}

Filter semantic: rows are emitted strictly when ``events.task_id =
<task_id>``. Global events (``task_id IS NULL``, e.g. the
``unknown_model_pricing`` row written by ``PricingTable``) are
intentionally skipped — including them in every per-task export would
double-count cross-task signals. A future ``trace export-global``
sibling can surface those.

JSONL is intentionally minimal — no envelope, no headers, easy to grep,
easy to pipe. Future formats (`prometheus`, `otlp`) layer on top
without changing this command's surface.
"""
from __future__ import annotations

import json
from typing import Any

import click

from hungerloop.cli.context import CliContext


@click.group("trace")
def trace() -> None:
    """LoopTrace and event-stream tooling."""


@trace.command("export")
@click.argument("task_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["jsonl"]),
    default="jsonl",
    show_default=True,
    help="Output format. Only 'jsonl' for now; reserved for prometheus/otlp.",
)
@click.pass_obj
def trace_export(ctx: CliContext, task_id: str, fmt: str) -> None:
    """Stream the event log for ``task_id`` to stdout as JSONL.

    Exit codes:
      * 0 — task exists (zero or more events emitted).
      * 1 — task is unknown.
    """
    if not _task_exists(ctx, task_id):
        click.echo(f"Task not found: {task_id}", err=True)
        raise click.exceptions.Exit(1)

    rows = _events_for_task(ctx, task_id)
    if not rows:
        return  # exit 0 with empty stdout

    for row in rows:
        click.echo(
            json.dumps(
                {
                    "task_id": row.get("task_id"),
                    "loop_id": row.get("loop_id"),
                    "event_type": row.get("event_type"),
                    "payload": row.get("payload", {}),
                    "created_at": row.get("created_at"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                # Coerce datetimes / sets / unknown objects to ``str``
                # rather than crashing mid-stream — a half-written JSONL
                # file is harder to recover from than a stringified
                # payload key.
                default=_jsonl_default,
            )
        )


def _events_for_task(ctx: CliContext, task_id: str) -> list[dict[str, Any]]:
    """Read events in append order; emit only rows whose ``task_id``
    matches ``task_id`` (``task_id IS NULL`` rows are global and
    excluded — see module docstring).

    Reaches into ``InMemoryRepository._events`` to keep this command
    working before SQLiteRepository lands (same pattern as
    ``report_format._resolve_goal_status``). The SQLite implementation
    will replace this with ``SELECT * FROM events WHERE task_id = ?
    ORDER BY created_at, event_id``.
    """
    raw_events: Any = getattr(ctx.repo, "_events", None)
    if not isinstance(raw_events, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in raw_events:
        if not isinstance(row, dict):
            continue
        if row.get("task_id") == task_id:
            rows.append(row)
    return rows


def _jsonl_default(value: object) -> str:
    """JSON encoder fallback: stringify everything else.

    Known callers only hand us primitives, but defensive serialization
    avoids the case where a future event payload smuggles in a
    ``datetime`` / ``set`` / custom object and crashes the export
    mid-file.
    """
    return str(value)


def _task_exists(ctx: CliContext, task_id: str) -> bool:
    """Same heuristic as ``report``/``status`` — any state row counts."""
    repo = ctx.repo
    for attr in (
        "_policies",
        "_ledgers",
        "_loop_traces",
        "_stop_reports_history",
    ):
        store: Any = getattr(repo, attr, None)
        if isinstance(store, dict) and task_id in store:
            return True
        if isinstance(store, dict):
            # Tracked as composite keys (task_id, loop_id) for loop_traces.
            for key in store:
                if isinstance(key, tuple) and key and key[0] == task_id:
                    return True
    # Last resort: any event already attached to the task.
    raw_events: Any = getattr(repo, "_events", None)
    if isinstance(raw_events, list):
        for row in raw_events:
            if isinstance(row, dict) and row.get("task_id") == task_id:
                return True
    return False
