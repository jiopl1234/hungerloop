"""``hungerloop mission`` — v0.6 mission-runtime operator commands."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

import click
from pydantic import ValidationError

from hungerloop.cli.context import CliContext
from hungerloop.cli.mission_cockpit import build_mission_cockpit, render_mission_cockpit
from hungerloop.cli.run_cmd import run as legacy_run
from hungerloop.cli.status_format import format_status
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.events import EventType
from hungerloop.models.mission import Mission, MissionFeature
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.mission_loader import (
    MissionLoader,
    MissionLoadError,
    ParsedMissionSpec,
)
from hungerloop.services.requirement_compiler import (
    MissionChangeResult,
    RequirementCompiler,
    RuleBasedCompiler,
)

_MISSION_NEW_CREATED = "MISSION_NEW_CREATED"
_MISSION_LOAD_FAILED = "MISSION_LOAD_FAILED"
_MISSION_IMPORT_APPLIED = "MISSION_IMPORT_APPLIED"
_MISSION_IMPORT_REJECTED = "MISSION_IMPORT_REJECTED"
_MISSION_IMPORT_FAILED = "MISSION_IMPORT_FAILED"
_MISSION_EDIT_APPLIED = "MISSION_EDIT_APPLIED"
_MISSION_EDIT_CANCELLED = "MISSION_EDIT_CANCELLED"
_MISSION_EDIT_NO_EDITOR = "MISSION_EDIT_NO_EDITOR"
# DEPRECATED, removable in v0.7.0: keep this RC rollback flag only so
# operators can force the v0.5f legacy path while validating v0.6.
_MISSION_RUNTIME_ENV = "HUNGERLOOP_MISSION_RUNTIME"
_MISSION_IMPORT_REQUIRES_PAUSED = (
    "mission import requires HUMAN_PAUSED state; use 'hungerloop hunger freeze' first"
)


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
        EventType.MISSION_CREATED,
        {
            "task_id": task_id,
            "mission_id": result.mission.mission_id,
            "feature_count": len(result.mission.features),
            "assertion_count": len(contract.assertions) if contract else 0,
        },
        task_id=task_id,
    )
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

    cockpit = build_mission_cockpit(ctx.repo, mission_obj)
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
@click.pass_obj
def mission_features(
    ctx: CliContext,
    task_id: str,
    phase_id: str | None,
    as_json: bool,
) -> None:
    """List mission features sorted by phase and feature id."""
    mission_obj = _require_mission(ctx, task_id)
    features = ctx.repo.list_mission_features(
        mission_id=mission_obj.mission_id,
        phase_id=phase_id,
    )
    if not features and mission_obj.features:
        features = [
            feature
            for feature in mission_obj.features
            if phase_id is None or feature.phase_id == phase_id
        ]
    rows = _sorted_features(features)
    if as_json:
        click.echo(
            json.dumps(
                [_feature_row(feature) for feature in rows],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        return
    click.echo(_render_feature_table(rows))


@mission.command("validation")
@click.argument("task_id")
@click.option("--phase", "phase_id", type=str, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def mission_validation(
    ctx: CliContext,
    task_id: str,
    phase_id: str | None,
    as_json: bool,
) -> None:
    """List validation contract assertions."""
    mission_obj = _require_mission(ctx, task_id)
    rows = _sorted_assertions(
        ctx.repo.list_validation_assertions(
            mission_id=mission_obj.mission_id,
            phase_id=phase_id,
        )
    )
    if as_json:
        click.echo(
            json.dumps(
                [_assertion_row(assertion) for assertion in rows],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        return
    click.echo(_render_validation_table(rows))


@mission.command("edit")
@click.argument("task_id")
@click.pass_obj
def mission_edit(ctx: CliContext, task_id: str) -> None:
    """Edit mission spec through ``$EDITOR`` and import the saved buffer."""
    mission_obj = _require_mission(ctx, task_id)
    if not _is_human_paused(ctx, task_id):
        _record_import_rejected(ctx, task_id, mission_obj)
        click.echo(_MISSION_IMPORT_REQUIRES_PAUSED, err=True)
        raise click.exceptions.Exit(7)

    editor = _resolve_editor(ctx, task_id, mission_obj)
    mission_md = _read_best_mission_markdown(ctx, task_id)
    with tempfile.TemporaryDirectory(prefix="hungerloop-mission-edit-") as raw_tmp:
        temp_dir = Path(raw_tmp)
        edit_path = temp_dir / "mission.md"
        edit_path.write_text(mission_md, encoding="utf-8")
        proc = subprocess.run([*shlex.split(editor), str(edit_path)], check=False)
        if proc.returncode != 0:
            _cancel_edit(
                ctx,
                task_id,
                mission_obj,
                reason="editor_exit_nonzero",
                exit_code=proc.returncode,
            )
            click.echo(
                f"mission edit cancelled: editor exited {proc.returncode}",
                err=True,
            )
            raise click.exceptions.Exit(1)
        saved = edit_path.read_text(encoding="utf-8")
        if not saved.strip():
            _cancel_edit(ctx, task_id, mission_obj, reason="empty_buffer")
            click.echo("mission edit cancelled: empty buffer", err=True)
            raise click.exceptions.Exit(1)
        result = _import_mission_from_path(
            ctx,
            task_id,
            edit_path,
            source="edit",
        )
    ctx.repo.append_event(
        _MISSION_EDIT_APPLIED,
        {
            "task_id": task_id,
            "mission_id": result.mission.mission_id,
            "evidence_id": result.evidence_id,
            "summary": result.summary,
        },
        task_id=task_id,
    )
    _print_change_summary(result.summary)


@mission.command("import")
@click.argument("task_id")
@click.option("--from", "from_path", type=click.Path(path_type=Path), required=True)
@click.pass_obj
def mission_import(ctx: CliContext, task_id: str, from_path: Path) -> None:
    """Import updated mission specs through the compiler write path."""
    result = _import_mission_from_path(ctx, task_id, from_path, source="import")
    _print_change_summary(result.summary)


def _require_mission(ctx: CliContext, task_id: str) -> Mission:
    mission_obj = ctx.repo.get_mission(task_id)
    if mission_obj is None:
        click.echo("task not found", err=True)
        raise click.exceptions.Exit(4)
    return mission_obj


def _sorted_features(features: list[MissionFeature]) -> list[MissionFeature]:
    return sorted(features, key=lambda feature: (feature.phase_id, feature.feature_id))


def _feature_row(feature: MissionFeature) -> dict[str, object]:
    return {
        "feature_id": feature.feature_id,
        "phase_id": feature.phase_id,
        "status": feature.status,
        "hunger_item_id": feature.hunger_item_id,
        "title": feature.title,
    }


def _render_feature_table(features: list[MissionFeature]) -> str:
    lines = ["phase_id feature_id status hunger_item_id title"]
    lines.extend(
        (
            f"{feature.phase_id} {feature.feature_id} {feature.status} "
            f"{feature.hunger_item_id} {feature.title}"
        )
        for feature in features
    )
    return "\n".join(lines)


def _sorted_assertions(
    assertions: list[ValidationAssertion],
) -> list[ValidationAssertion]:
    return sorted(
        assertions,
        key=lambda assertion: (assertion.phase_id, assertion.assertion_id),
    )


def _assertion_row(assertion: ValidationAssertion) -> dict[str, object]:
    return {
        "assertion_id": assertion.assertion_id,
        "phase_id": assertion.phase_id,
        "status": assertion.status,
        "last_loop": assertion.validated_at_loop,
        "title": assertion.title,
    }


def _render_validation_table(assertions: list[ValidationAssertion]) -> str:
    lines = ["assertion_id phase_id status last_loop title"]
    lines.extend(
        (
            f"{assertion.assertion_id} {assertion.phase_id} {assertion.status} "
            f"{assertion.validated_at_loop if assertion.validated_at_loop is not None else '-'} "
            f"{assertion.title}"
        )
        for assertion in assertions
    )
    return "\n".join(lines)


def _is_human_paused(ctx: CliContext, task_id: str) -> bool:
    task = ctx.repo.get_task(task_id)
    return task is not None and task.status == "HUMAN_PAUSED"


def _record_import_rejected(
    ctx: CliContext,
    task_id: str,
    mission_obj: Mission | None,
) -> None:
    ctx.repo.append_event(
        _MISSION_IMPORT_REJECTED,
        {
            "task_id": task_id,
            "mission_id": mission_obj.mission_id if mission_obj is not None else None,
            "reason": "not_human_paused",
        },
        task_id=task_id,
    )


def _resolve_editor(ctx: CliContext, task_id: str, mission_obj: Mission) -> str:
    editor = os.environ.get("EDITOR")
    if editor and editor.strip():
        return editor
    vi = shutil.which("vi")
    if vi:
        return vi
    ctx.repo.append_event(
        _MISSION_EDIT_NO_EDITOR,
        {
            "task_id": task_id,
            "mission_id": mission_obj.mission_id,
            "reason": "no_editor",
        },
        task_id=task_id,
    )
    click.echo("No editor found; set EDITOR or install vi.", err=True)
    raise click.exceptions.Exit(6)


def _read_best_mission_markdown(ctx: CliContext, task_id: str) -> str:
    path = ctx.workspace_root / "tasks" / task_id / "best" / "files" / "mission.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    mission_obj = _require_mission(ctx, task_id)
    return _fallback_mission_markdown(
        mission_obj,
        ctx.repo.get_validation_contract(mission_obj.mission_id),
    )


def _fallback_mission_markdown(
    mission_obj: Mission,
    contract: ValidationContract | None,
) -> str:
    assertions_by_phase: dict[str, list[ValidationAssertion]] = {}
    if contract is not None:
        for assertion in contract.assertions:
            assertions_by_phase.setdefault(assertion.phase_id, []).append(assertion)
    features_by_phase: dict[str, list[MissionFeature]] = {}
    for feature in mission_obj.features:
        features_by_phase.setdefault(feature.phase_id, []).append(feature)

    lines = [
        f"# {mission_obj.title}",
        "",
        "## Description",
        "",
        mission_obj.description,
        "",
        "## Phases",
        "",
    ]
    for phase in mission_obj.phases:
        lines.extend(
            [
                f"### {phase.phase_id} {phase.title}",
                "",
                f"Status: `{phase.status}`",
                "",
                phase.description,
                "",
                "Features:",
            ]
        )
        phase_features = _sorted_features(features_by_phase.get(phase.phase_id, []))
        if phase_features:
            lines.extend(
                (
                    f"- [{feature.status}] Feature {feature.feature_id}: "
                    f"{feature.title} (hunger: {feature.hunger_item_id})"
                )
                for feature in phase_features
            )
        else:
            lines.append("- None")
        lines.extend(["", "Assertions:"])
        phase_assertions = _sorted_assertions(
            assertions_by_phase.get(phase.phase_id, [])
        )
        if phase_assertions:
            lines.extend(
                (
                    f"- [{assertion.status}] Assertion {assertion.assertion_id}: "
                    f"{assertion.title} ({assertion.check_type})"
                )
                for assertion in phase_assertions
            )
        else:
            lines.append("- None")
        lines.extend(["", ""])
    return "\n".join(lines)


def _cancel_edit(
    ctx: CliContext,
    task_id: str,
    mission_obj: Mission,
    *,
    reason: str,
    exit_code: int | None = None,
) -> None:
    payload: dict[str, object] = {
        "task_id": task_id,
        "mission_id": mission_obj.mission_id,
        "reason": reason,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    ctx.repo.append_event(_MISSION_EDIT_CANCELLED, payload, task_id=task_id)


def _import_mission_from_path(
    ctx: CliContext,
    task_id: str,
    from_path: Path,
    *,
    source: str,
) -> MissionChangeResult:
    mission_obj = ctx.repo.get_mission(task_id)
    if not _is_human_paused(ctx, task_id):
        _record_import_rejected(ctx, task_id, mission_obj)
        click.echo(_MISSION_IMPORT_REQUIRES_PAUSED, err=True)
        raise click.exceptions.Exit(7)
    try:
        parsed = MissionLoader().load_from_path(from_path)
        result = RequirementCompiler(ctx.repo).compile_mission_changes(task_id, parsed)
    except (MissionLoadError, OSError, ValidationError, ValueError) as exc:
        ctx.repo.append_event(
            _MISSION_LOAD_FAILED,
            {"task_id": task_id, "error": str(exc), "source": source},
            task_id=task_id,
        )
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(2) from exc
    except Exception as exc:
        ctx.repo.append_event(
            _MISSION_IMPORT_FAILED,
            {"task_id": task_id, "error": str(exc), "source": source},
            task_id=task_id,
        )
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1) from exc
    ctx.repo.append_event(
        _MISSION_IMPORT_APPLIED,
        {
            "task_id": task_id,
            "mission_id": result.mission.mission_id,
            "evidence_id": result.evidence_id,
            "summary": result.summary,
            "source": source,
        },
        task_id=task_id,
    )
    return result


def _print_change_summary(summary: dict[str, int]) -> None:
    click.echo(
        f"{summary.get('features_added', 0)} features added, "
        f"{summary.get('assertions_added', 0)} assertions added"
    )


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
