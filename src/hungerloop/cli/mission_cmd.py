"""``hungerloop mission`` — v0.6 mission-runtime operator commands."""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click
from pydantic import ValidationError

from hungerloop.cli.context import CliContext
from hungerloop.cli.run_cmd import run as legacy_run
from hungerloop.cli.status_format import format_status
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.mission_loader import (
    MissionLoader,
    MissionLoadError,
    ParsedMissionSpec,
)
from hungerloop.services.requirement_compiler import (
    RequirementCompiler,
    RuleBasedCompiler,
)

_MISSION_NEW_CREATED = "MISSION_NEW_CREATED"
_MISSION_LOAD_FAILED = "MISSION_LOAD_FAILED"
_MISSION_RUNTIME_ENV = "HUNGERLOOP_MISSION_RUNTIME"


@click.group("mission")
def mission() -> None:
    """Mission-runtime commands."""


@mission.command("new")
@click.argument("task_id")
@click.option("--goal", "goal", type=str, default=None, help="Mission goal text.")
@click.option(
    "--from",
    "from_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Mission spec directory or mission.md file.",
)
@click.option(
    "--contract",
    "contract_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Validation contract YAML file overriding the spec directory contract.",
)
@click.option(
    "--accept",
    "accept_specs",
    multiple=True,
    help="Legacy v0.5f acceptance-check JSON. Supplying this skips mission insertion.",
)
@click.pass_obj
def mission_new(
    ctx: CliContext,
    task_id: str,
    goal: str | None,
    from_path: Path | None,
    contract_path: Path | None,
    accept_specs: tuple[str, ...],
) -> None:
    """Create a new mission task or route to the legacy ``new`` compiler."""
    if accept_specs:
        _create_legacy_task(ctx, task_id, goal, accept_specs)
        return

    if ctx.repo.get_mission(task_id) is not None:
        click.echo(
            "mission already exists for task; use 'mission import' to update",
            err=True,
        )
        raise click.exceptions.Exit(2)

    try:
        parsed = _load_new_mission_spec(
            from_path=from_path,
            contract_path=contract_path,
            goal=goal,
        )
    except (MissionLoadError, OSError, ValidationError) as exc:
        ctx.repo.append_event(
            _MISSION_LOAD_FAILED,
            {"task_id": task_id, "error": str(exc)},
        )
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(2) from exc

    ctx.repo.create_task(task_id, goal or parsed.description or parsed.title)
    result = RequirementCompiler(ctx.repo).compile_new_mission(task_id, parsed)
    contract = result.validation_contract
    ctx.repo.append_event(
        _MISSION_NEW_CREATED,
        {
            "task_id": task_id,
            "mission_id": result.mission.mission_id,
            "feature_count": len(result.mission.features),
            "assertion_count": len(contract.assertions) if contract else 0,
        },
        task_id=task_id,
    )

    click.echo(
        "Created mission: "
        f"task_id={task_id} mission_id={result.mission.mission_id} "
        f"features={len(result.mission.features)}"
    )


@mission.command("run")
@click.argument("task_id")
@click.option(
    "--max-loops",
    type=int,
    default=200,
    show_default=True,
    help="Defensive cap; HungerEngine normally terminates the loop earlier.",
)
@click.option(
    "--refill",
    "refill_loops",
    type=int,
    default=None,
    help="Credit N loop budgets before resuming.",
)
@click.option(
    "--spend-budget",
    is_flag=True,
    default=False,
    help="Continue into deterministic refinement tiers after base correctness.",
)
@click.option(
    "--refinement-profile",
    type=str,
    default=None,
    help="Refinement profile to use with --spend-budget, e.g. python_medium.",
)
@click.option(
    "--resume",
    "resume_human",
    is_flag=True,
    default=False,
    help="Confirm that HUMAN_REQUIRED / HUMAN_PAUSED preconditions are resolved.",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Required to start a new run after a DONE stop_reason.",
)
@click.option(
    "--skip-repair-check",
    is_flag=True,
    default=False,
    help="Bypass the ERROR-recovery gate after repair-state has been inspected.",
)
@click.pass_context
def mission_run(
    click_ctx: click.Context,
    task_id: str,
    max_loops: int,
    refill_loops: int | None,
    spend_budget: bool,
    refinement_profile: str | None,
    resume_human: bool,
    reset: bool,
    skip_repair_check: bool,
) -> None:
    """Run a mission task through the legacy ``hungerloop run`` command."""
    if os.environ.get(_MISSION_RUNTIME_ENV) == "0":
        ctx = _require_cli_context(click_ctx)
        original_obj = click_ctx.obj
        click_ctx.obj = CliContext(
            repo=cast(RepositoryProtocol, _MissionHiddenRepository(ctx.repo)),
            workspace_root=ctx.workspace_root,
            model_client=ctx.model_client,
            extras=ctx.extras,
        )
        try:
            click.echo(
                f"{_MISSION_RUNTIME_ENV}=0 is deprecated and will be removed in "
                "v0.7.0; running legacy path."
            )
            click_ctx.invoke(
                legacy_run,
                task_id=task_id,
                max_loops=max_loops,
                refill_loops=refill_loops,
                budget_loops=None,
                spend_budget=spend_budget,
                refinement_profile=refinement_profile,
                max_refinement_tier=0,
                ignore_stagnation=False,
                unblock_all=False,
                resume_human=resume_human,
                raise_cost_ceiling=None,
                steal_lock=False,
                lock_stale_sec=None,
                model_config_path=None,
                accept_unknown_pricing=False,
                reset=reset,
                skip_repair_check=skip_repair_check,
            )
        finally:
            click_ctx.obj = original_obj
        return

    click_ctx.invoke(
        legacy_run,
        task_id=task_id,
        max_loops=max_loops,
        refill_loops=refill_loops,
        budget_loops=None,
        spend_budget=spend_budget,
        refinement_profile=refinement_profile,
        max_refinement_tier=0,
        ignore_stagnation=False,
        unblock_all=False,
        resume_human=resume_human,
        raise_cost_ceiling=None,
        steal_lock=False,
        lock_stale_sec=None,
        model_config_path=None,
        accept_unknown_pricing=False,
        reset=reset,
        skip_repair_check=skip_repair_check,
    )


@mission.command("status")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
@click.pass_obj
def mission_status(ctx: CliContext, task_id: str, as_json: bool) -> None:
    """Print mission cockpit status, or legacy status when no mission exists."""
    mission_obj = ctx.repo.get_mission(task_id)
    if mission_obj is None:
        click.echo(format_status(ctx.repo, task_id))
        return

    cockpit = build_mission_cockpit(ctx, mission_obj)
    if as_json:
        click.echo(
            json.dumps(
                cockpit.to_json_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        return
    click.echo(render_mission_cockpit(cockpit))


@mission.command("features")
@click.argument("task_id")
@click.option("--phase", "phase_id", type=str, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
def mission_features(task_id: str, phase_id: str | None, as_json: bool) -> None:
    """List mission features. Implemented in a later M6 feature."""
    del task_id, phase_id, as_json
    raise click.ClickException("mission features is not implemented yet")


@mission.command("validation")
@click.argument("task_id")
@click.option("--phase", "phase_id", type=str, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
def mission_validation(task_id: str, phase_id: str | None, as_json: bool) -> None:
    """List validation assertions. Implemented in a later M6 feature."""
    del task_id, phase_id, as_json
    raise click.ClickException("mission validation is not implemented yet")


@mission.command("edit")
@click.argument("task_id")
def mission_edit(task_id: str) -> None:
    """Edit mission spec through ``$EDITOR``. Implemented in a later M6 feature."""
    del task_id
    raise click.ClickException("mission edit is not implemented yet")


@mission.command("import")
@click.argument("task_id")
@click.option("--from", "from_path", type=click.Path(path_type=Path), required=True)
def mission_import(task_id: str, from_path: Path) -> None:
    """Import updated mission specs. Implemented in a later M6 feature."""
    del task_id, from_path
    raise click.ClickException("mission import is not implemented yet")


@dataclass(frozen=True)
class _StatusRow:
    id: str
    title: str
    status: str
    symbol: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "symbol": self.symbol,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class _MissionCockpit:
    mission: Mission
    phases: list[_StatusRow]
    active_phase: MissionPhase | None
    features_in_active_phase: list[_StatusRow]
    validation_contract: dict[str, int]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "mission": self.mission.model_dump(mode="json"),
            "phases": [row.to_json() for row in self.phases],
            "features_in_active_phase": [
                row.to_json() for row in self.features_in_active_phase
            ],
            "validation_contract": dict(self.validation_contract),
        }


def build_mission_cockpit(ctx: CliContext, mission_obj: Mission) -> _MissionCockpit:
    """Collect mission status rows for text and JSON renderers."""
    features = ctx.repo.list_mission_features(mission_id=mission_obj.mission_id)
    if not features and mission_obj.features:
        features = mission_obj.features
    features_by_phase: dict[str, list[MissionFeature]] = {}
    for feature in features:
        features_by_phase.setdefault(feature.phase_id, []).append(feature)

    contract = ctx.repo.get_validation_contract(mission_obj.mission_id)
    assertions = contract.assertions if contract is not None else []
    assertions_by_phase: dict[str, list[ValidationAssertion]] = {}
    for assertion in assertions:
        assertions_by_phase.setdefault(assertion.phase_id, []).append(assertion)

    phases = ctx.repo.list_mission_phases(mission_obj.mission_id)
    if not phases and mission_obj.phases:
        phases = mission_obj.phases
    phase_rows = [
        _StatusRow(
            id=phase.phase_id,
            title=phase.title,
            status=phase.status,
            symbol=_status_symbol(phase.status),
            detail=_phase_detail(
                phase,
                features_by_phase.get(phase.phase_id, []),
                assertions_by_phase.get(phase.phase_id, []),
            ),
        )
        for phase in phases
    ]
    active_phase = _active_phase(phases)
    active_features = (
        features_by_phase.get(active_phase.phase_id, []) if active_phase else []
    )
    feature_rows = [
        _StatusRow(
            id=feature.feature_id,
            title=feature.title,
            status=feature.status,
            symbol=_status_symbol(feature.status),
            detail=_feature_detail(feature),
        )
        for feature in active_features
    ]
    return _MissionCockpit(
        mission=mission_obj,
        phases=phase_rows,
        active_phase=active_phase,
        features_in_active_phase=feature_rows,
        validation_contract=_contract_summary(assertions),
    )


def render_mission_cockpit(cockpit: _MissionCockpit) -> str:
    """Render the human mission cockpit specified by REQ-M6-020."""
    lines = [
        f"Mission: {cockpit.mission.mission_id} — {cockpit.mission.title}",
        "Phases:",
    ]
    for row in cockpit.phases:
        detail = f"            {row.detail}" if row.detail else ""
        lines.append(f"  {row.symbol} {row.id} — {row.title}{detail}")

    lines.append("")
    lines.append("Features in active phase:")
    if cockpit.features_in_active_phase:
        for row in cockpit.features_in_active_phase:
            detail = f"           {row.detail}" if row.detail else ""
            lines.append(f"  {row.symbol} {row.id}   {row.title}{detail}")
    else:
        lines.append("  (none)")

    summary = cockpit.validation_contract
    lines.extend(
        [
            "",
            "Validation contract:",
            (
                f"  Pending: {summary['pending']}    "
                f"Passed: {summary['passed']}    "
                f"Failed: {summary['failed']}    "
                f"Blocked: {summary['blocked']}"
            ),
        ]
    )
    return "\n".join(lines)


def _create_legacy_task(
    ctx: CliContext,
    task_id: str,
    goal: str | None,
    accept_specs: tuple[str, ...],
) -> None:
    parsed_checks = [_parse_legacy_accept_spec(raw) for raw in accept_specs]
    final_goal = goal or task_id
    ledger_hints = {
        "core_acceptance_checks": parsed_checks,
        "core_acceptance_mode": "all",
        "enable_memory_consolidation": False,
    }
    _, ledger = RuleBasedCompiler().compile(task_id, final_goal, hints=ledger_hints)
    ctx.repo.create_task(task_id, final_goal)
    ctx.repo.set_hunger_policy(task_id, ctx.repo.get_hunger_policy(task_id))
    ctx.repo.save_hunger_ledger(task_id, ledger)
    click.echo(f"Created task: {task_id}")
    click.echo(f"  task_id={task_id}")
    click.echo("  legacy_acceptance: true")


def _parse_legacy_accept_spec(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"--accept must be valid JSON; got: {raw}") from exc
    if not isinstance(parsed, dict):
        raise click.UsageError(
            f"--accept JSON must be an object, got {type(parsed).__name__}"
        )
    if "check_type" in parsed:
        return dict(parsed)
    if "check_keys" in parsed:
        return {
            "check_type": AcceptanceCheckType.EVIDENCE_COUNT_MIN.value,
            "params": {"evidence_type": "any", "min_count": 1},
            "description": f"Legacy acceptance keys: {parsed['check_keys']}",
        }
    return dict(parsed)


def _load_new_mission_spec(
    *,
    from_path: Path | None,
    contract_path: Path | None,
    goal: str | None,
) -> ParsedMissionSpec:
    loader = MissionLoader()
    if from_path is not None:
        parsed = loader.load_from_path(from_path)
    else:
        if goal is None:
            raise MissionLoadError("--goal is required when --from is not supplied")
        parsed = ParsedMissionSpec(
            title=goal,
            description=goal,
            phases=[],
            features=[],
            validation_contract=None,
            source_files=[],
            mission_markdown_loaded=True,
        )

    if contract_path is not None:
        resolved_contract_path = contract_path.resolve(strict=True)
        contract = loader._load_validation_contract(resolved_contract_path)
        parsed = parsed.model_copy(
            update={
                "validation_contract": contract,
                "source_files": sorted(
                    {*parsed.source_files, resolved_contract_path.name}
                ),
            }
        )
    if goal is not None:
        parsed = parsed.model_copy(
            update={
                "title": parsed.title if from_path is not None else goal,
                "description": parsed.description or goal,
            }
        )
    return parsed


def _active_phase(phases: list[MissionPhase]) -> MissionPhase | None:
    for status in ("validating", "in_progress"):
        for phase in phases:
            if phase.status == status:
                return phase
    for phase in phases:
        if phase.status == "pending":
            return phase
    return phases[-1] if phases else None


def _status_symbol(status: str) -> str:
    if status in {"done", "passed"}:
        return "[✓]"
    if status in {"in_progress", "validating"}:
        return "[→]"
    if status in {"failed", "blocked"}:
        return "[×]"
    return "[ ]"


def _phase_detail(
    phase: MissionPhase,
    features: list[MissionFeature],
    assertions: list[ValidationAssertion],
) -> str:
    if phase.status == "done":
        loops = [
            assertion.validated_at_loop
            for assertion in assertions
            if assertion.validated_at_loop is not None
        ]
        if loops:
            return f"validated at loop {max(loops)}"
        return "done"
    if phase.status == "validating":
        done_features = sum(1 for feature in features if feature.status == "done")
        return f"validating ({done_features}/{len(features)} features done)"
    return phase.status


def _feature_detail(feature: MissionFeature) -> str:
    worker = feature.assigned_worker_ids[-1] if feature.assigned_worker_ids else ""
    if feature.status == "blocked":
        return "BLOCKED — see handoff_items[0]"
    if feature.status == "in_progress":
        return f"{worker} (handoff pending)" if worker else "handoff pending"
    if feature.status == "done":
        return worker or "done"
    return worker or feature.status


def _contract_summary(assertions: list[ValidationAssertion]) -> dict[str, int]:
    counts = Counter(assertion.status for assertion in assertions)
    return {
        "pending": counts.get("pending", 0),
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "blocked": counts.get("blocked", 0),
    }


def _require_cli_context(click_ctx: click.Context) -> CliContext:
    ctx = click_ctx.obj
    if not isinstance(ctx, CliContext):
        raise click.ClickException("missing CLI context")
    return ctx


class _MissionHiddenRepository:
    """Repository proxy that disables mission lookup for legacy runtime mode."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self._repo = repo

    def get_mission(self, task_id: str) -> Mission | None:
        del task_id
        return None

    def append_event(
        self,
        event_type: object,
        payload: dict[str, object],
        *,
        task_id: str | None = None,
        loop_id: int | None = None,
    ) -> None:
        actual_event_type = (
            event_type.value
            if hasattr(event_type, "value")
            else str(event_type)
        )
        if actual_event_type.startswith("mission."):
            return
        self._repo.append_event(
            actual_event_type,
            payload,
            task_id=task_id,
            loop_id=loop_id,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)
