"""Build the ``hungerloop report`` JSON v1 dict and markdown rendering.

The two formatters are deliberately independent (PRD §18.3, decision
§11.1): JSON is the wire contract for downstream tools; markdown is for
PR comments / chat. They share data sources but not output templates.
"""
from __future__ import annotations

from typing import Any

from hungerloop.cli.mission_cockpit import build_mission_cockpit, render_mission_cockpit
from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.tracing import LoopTrace
from hungerloop.repository.protocol import RepositoryProtocol

REPORT_SCHEMA_VERSION = "1"


def build_report_dict(
    repo: RepositoryProtocol, task_id: str
) -> dict[str, Any]:
    """Return the JSON-shaped report dict per PRD §18.3 schema v1.

    All keys are always present; absent values are ``null`` or ``[]``.
    """
    last_stop = repo.get_last_stop_reason(task_id)
    usage = repo.get_usage_snapshot(task_id)
    best = repo.get_best_state(task_id)
    ledger = repo.get_hunger_ledger(task_id)
    traces = repo.list_loop_traces(task_id)

    items_open = sum(
        1
        for item in ledger.items
        if item.status in (HungerItemStatus.OPEN, HungerItemStatus.WORKING)
    )
    items_blocked = sum(
        1 for item in ledger.items if item.status == HungerItemStatus.BLOCKED
    )
    items_satisfied = sum(
        1
        for item in ledger.items
        if item.status
        in (HungerItemStatus.CLOSED, HungerItemStatus.VALIDATED_SATISFIED)
    )

    loops_committed = sum(1 for t in traces if t.committed)
    loops_rejected = sum(1 for t in traces if not t.committed)

    last_loop_block: dict[str, Any] | None = None
    if traces:
        last = traces[-1]
        last_loop_block = {
            "loop_id": last.loop_id,
            "phase": last.phase,
            "delta_summary": last.delta_summary,
        }

    # Goal is sourced from TaskRecord (§3.4) when SQLiteRepository lands.
    # For InMemoryRepository there is no canonical store; emit null.
    goal = _resolve_goal(repo, task_id)

    if last_stop is None:
        stop_block: dict[str, Any] = {
            "reason": None,
            "goal_status": "in_progress",
            "recommendation": "",
        }
    else:
        stop_block = {
            "reason": last_stop.value,
            "goal_status": _resolve_goal_status(repo, task_id),
            "recommendation": _resolve_recommendation(repo, task_id),
        }

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "task_id": task_id,
        "goal": goal,
        "stop": stop_block,
        "ledger": {
            "items_total": len(ledger.items),
            "items_satisfied": items_satisfied,
            "items_blocked": items_blocked,
            "items_open": items_open,
        },
        "usage": {
            "tokens": usage.tokens,
            "cost_usd": round(usage.cost_usd, 6),
            "llm_calls": usage.llm_calls,
            "tool_calls": usage.tool_calls,
        },
        "loops": {
            "total": len(traces),
            "committed": loops_committed,
            "rejected": loops_rejected,
        },
        "best_state_id": best.state_id if best else None,
        "accepted_check_keys": list(best.accepted_check_keys) if best else [],
        "last_loop": last_loop_block,
    }
    mission = repo.get_mission(task_id)
    if mission is not None:
        cockpit = build_mission_cockpit(repo, mission)
        report["mission_cockpit"] = cockpit.to_json_payload()
        report["mission_cockpit_text"] = render_mission_cockpit(cockpit)
    return report


def format_markdown(report: dict[str, Any]) -> str:
    """Render the report dict as a human-readable markdown summary.

    Distinct template from ``hungerloop status`` per decision §11.1: the
    two formats evolve independently. Sections: headline, Goal, Stop,
    Ledger, Usage, Loops, Last loop.
    """
    stop = report["stop"]
    headline_status = stop["reason"] if stop["reason"] else "running"
    lines: list[str] = [
        f"# Task `{report['task_id']}` — {headline_status}",
        "",
    ]

    goal = report.get("goal")
    if goal:
        lines.append(f"**Goal**: {goal}")
        lines.append("")

    lines.extend(
        [
            "## Stop",
            f"- reason: `{stop['reason'] or '(running)'}`",
            f"- goal_status: `{stop['goal_status']}`",
        ]
    )
    if stop["recommendation"]:
        lines.append(f"- recommendation: {stop['recommendation']}")
    lines.append("")

    ledger = report["ledger"]
    lines.extend(
        [
            "## Ledger",
            f"- total: {ledger['items_total']}",
            f"- satisfied: {ledger['items_satisfied']}",
            f"- blocked: {ledger['items_blocked']}",
            f"- open: {ledger['items_open']}",
            "",
        ]
    )

    usage = report["usage"]
    lines.extend(
        [
            "## Usage",
            f"- tokens: {usage['tokens']}",
            f"- cost_usd: {usage['cost_usd']:.4f}",
            f"- llm_calls: {usage['llm_calls']}",
            f"- tool_calls: {usage['tool_calls']}",
            "",
        ]
    )

    loops = report["loops"]
    lines.extend(
        [
            "## Loops",
            f"- total: {loops['total']}",
            f"- committed: {loops['committed']}",
            f"- rejected: {loops['rejected']}",
            "",
        ]
    )

    last_loop = report["last_loop"]
    if last_loop:
        lines.extend(
            [
                "## Last loop",
                f"- loop_id: {last_loop['loop_id']}",
                f"- phase: {last_loop['phase']}",
                f"- delta_summary: {last_loop['delta_summary']}",
                "",
            ]
        )

    accepted = report["accepted_check_keys"]
    if accepted:
        lines.append("## Accepted check keys")
        for key in accepted:
            lines.append(f"- `{key}`")
        lines.append("")

    mission_cockpit_text = report.get("mission_cockpit_text")
    if mission_cockpit_text:
        lines.extend(
            [
                "## Mission cockpit",
                str(mission_cockpit_text).rstrip(),
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _resolve_goal(repo: RepositoryProtocol, task_id: str) -> str | None:
    """Return persisted task goal when available."""
    record = repo.get_task(task_id)
    if record is not None and record.raw_goal:
        return record.raw_goal
    return None


def _resolve_goal_status(repo: RepositoryProtocol, task_id: str) -> str:
    """Read the persisted StopReport's goal_status if available; default
    to ``unknown`` so callers always have a string to switch on."""
    report = repo.get_last_stop_report(task_id)
    if report is not None:
        return report.goal_status
    return "unknown"


def _resolve_recommendation(repo: RepositoryProtocol, task_id: str) -> str:
    report = repo.get_last_stop_report(task_id)
    if report is not None:
        return report.recommendation
    return ""


def _last_trace(repo: RepositoryProtocol, task_id: str) -> LoopTrace | None:
    """Convenience for callers that only need the last trace; shared
    with future trace-export tooling."""
    traces = repo.list_loop_traces(task_id)
    return traces[-1] if traces else None
