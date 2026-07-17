"""``hungerloop run`` — preflight, orchestrate, persist StopReport."""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path

import click

from hungerloop.cli.context import CliContext
from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.cli.preflight import PreflightError, check_resume_preflight
from hungerloop.models.enums import CompletionMode, DecayType
from hungerloop.models.events import EventType
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_allocator import BudgetAllocator
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.loop_orchestrator import _SpecSynthesizerProtocol
from hungerloop.services.model_client import DummyModelClient, ModelAuthError, ModelClient
from hungerloop.services.model_config import (
    ModelConfigLoader,
    ModelProvider,
    PricingTable,
)
from hungerloop.services.openai_model_client import OpenAIModelClient
from hungerloop.services.skill_manager import SkillManager

DEFAULT_LOCK_STALE_SEC = 30 * 60  # 30 minutes


def _build_lock_owner() -> str:
    """Return ``hostname:pid:uuid8``; recorded so a human can identify
    which terminal/host owns a held lock (PRD §5.1.1)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _resolve_stale_threshold(cli_value: int | None) -> int:
    """CLI flag wins over env; env wins over the 30-min default."""
    if cli_value is not None:
        return cli_value
    raw = os.environ.get("HUNGERLOOP_LOCK_STALE_SEC")
    if raw is None:
        return DEFAULT_LOCK_STALE_SEC
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_LOCK_STALE_SEC


@click.command("run")
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
    help=(
        "Credit N loop budgets before resuming; required when the previous "
        "stop_reason was HUNGER_EXPIRED."
    ),
)
@click.option(
    "--budget-loops",
    type=int,
    default=None,
    help="Explicit loop work budget for this run.",
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
    "--max-refinement-tier",
    type=int,
    default=0,
    show_default=True,
    help="Highest deterministic refinement tier to generate.",
)
@click.option(
    "--ignore-stagnation",
    is_flag=True,
    default=False,
    help="In spend-budget mode, keep running until budget exhaustion.",
)
@click.option(
    "--draft-k",
    "draft_k",
    type=click.IntRange(1, 5),
    default=None,
    help="Cold-start draft sampling count (first loop only; 1 disables, max 5).",
)
@click.option(
    "--unblock-all",
    is_flag=True,
    default=False,
    help="Reset every BLOCKED hunger item to OPEN; required after BLOCKED stop.",
)
@click.option(
    "--resume",
    "resume_human",
    is_flag=True,
    default=False,
    help="Confirm that HUMAN_REQUIRED / HUMAN_PAUSED preconditions are resolved.",
)
@click.option(
    "--raise-cost-ceiling",
    "raise_cost_ceiling",
    type=float,
    default=None,
    help=(
        "New max_total_cost_usd; required when the previous stop_reason "
        "was SAFETY_STOP."
    ),
)
@click.option(
    "--steal-lock",
    "steal_lock",
    is_flag=True,
    default=False,
    help=(
        "Force-take the task lock from a stale or live owner. "
        "Audit-logged via the lock_stolen event."
    ),
)
@click.option(
    "--lock-stale-sec",
    "lock_stale_sec",
    type=int,
    default=None,
    help=(
        "Stale-lock threshold in seconds (overrides "
        "HUNGERLOOP_LOCK_STALE_SEC; default 1800 = 30 min)."
    ),
)
@click.option(
    "--model-config",
    "model_config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="YAML model config. Supports provider: dummy or openai.",
)
@click.option(
    "--accept-unknown-pricing",
    is_flag=True,
    default=False,
    help=(
        "Allow openai model names that are not in PricingTable.PRICES. "
        "Cost will be recorded as 0.0 except for token ceilings."
    ),
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help=(
        "Required to start a new run after a DONE stop_reason. "
        "Wipes the prior best_state references for this task."
    ),
)
@click.option(
    "--skip-repair-check",
    is_flag=True,
    default=False,
    help=(
        "Bypass the ERROR-recovery gate. Audit-logged via "
        "repair_state_action with action=\"skipped\". Use only after "
        "repair-state surfaces no fixable divergences."
    ),
)
@click.pass_obj
def run(
    ctx: CliContext,
    task_id: str,
    max_loops: int,
    refill_loops: int | None,
    budget_loops: int | None,
    spend_budget: bool,
    refinement_profile: str | None,
    max_refinement_tier: int,
    ignore_stagnation: bool,
    draft_k: int | None,
    unblock_all: bool,
    resume_human: bool,
    raise_cost_ceiling: float | None,
    steal_lock: bool,
    lock_stale_sec: int | None,
    model_config_path: Path | None,
    accept_unknown_pricing: bool,
    reset: bool,
    skip_repair_check: bool,
) -> None:
    """Drive ``task_id`` through the orchestrator until a StopReport.

    Runs the preflight checks (PRD §18.3) before invoking the orchestrator,
    applies any requested refill / unblock / cost-ceiling raises *before*
    preflight, then persists the resulting StopReport per §28.16 / M4.
    """
    # FR-3 audit: write the override event *before* the preflight so the
    # gate query sees it and accepts the resume.
    if skip_repair_check:
        ctx.repo.append_event(
            EventType.REPAIR_STATE_ACTION,
            {
                "action": "skipped",
                "reason": "operator override (--skip-repair-check)",
            },
            task_id=task_id,
        )

    _validate_budgeted_refinement_flags(
        max_loops=max_loops,
        budget_loops=budget_loops,
        spend_budget=spend_budget,
        refinement_profile=refinement_profile,
        max_refinement_tier=max_refinement_tier,
        ignore_stagnation=ignore_stagnation,
    )

    # Preflight runs *before* overrides so its comparisons (e.g.
    # raise_cost_ceiling vs. current ceiling) see the pre-resume policy.
    try:
        check_resume_preflight(
            ctx.repo,
            task_id,
            refill_loops=refill_loops,
            unblock_all=unblock_all,
            resume_human=resume_human,
            raise_cost_ceiling=raise_cost_ceiling,
            reset=reset,
            skip_repair_check=skip_repair_check,
        )
    except PreflightError as exc:
        click.echo(f"Preflight error: {exc}", err=True)
        raise click.exceptions.Exit(2) from exc

    _apply_user_overrides(
        ctx,
        task_id,
        refill_loops=refill_loops,
        budget_loops=budget_loops,
        spend_budget=spend_budget,
        refinement_profile=refinement_profile,
        max_refinement_tier=max_refinement_tier,
        ignore_stagnation=ignore_stagnation,
        draft_k=draft_k,
        unblock_all=unblock_all,
        resume_human=resume_human,
        raise_cost_ceiling=raise_cost_ceiling,
    )

    try:
        model_client = _resolve_model_client(
            ctx,
            model_config_path,
            accept_unknown_pricing=accept_unknown_pricing,
        )
    except (ValueError, NotImplementedError, ModelAuthError) as exc:
        raise click.ClickException(str(exc)) from exc
    budget_allocator = _resolve_budget_allocator(model_config_path)

    # Acquire the task lock (PRD §5.1.1) before invoking the orchestrator.
    owner = _build_lock_owner()
    stale_threshold = _resolve_stale_threshold(lock_stale_sec)
    outcome = ctx.repo.acquire_task_lock(
        task_id,
        owner,
        stale_threshold_seconds=stale_threshold,
        steal=steal_lock,
    )
    if outcome == "held_live":
        click.echo(
            f"Task {task_id} is locked by another live process. "
            f"Wait or pass --steal-lock if you've verified the owner is dead.",
            err=True,
        )
        raise click.exceptions.Exit(3)
    if outcome == "held_stale":
        click.echo(
            f"Task {task_id} has a stale lock "
            f"(idle ≥ {stale_threshold}s). "
            f"Pass --steal-lock to force-take it.",
            err=True,
        )
        raise click.exceptions.Exit(6)
    # outcome ∈ {acquired, reentrant, stolen} — proceed.

    try:
        spec_synthesizer = _build_spec_check_synthesizer(
            repo=ctx.repo, task_id=task_id
        )
        orchestrator = build_orchestrator(
            repo=ctx.repo,
            workspace_root=ctx.workspace_root,
            model_client=model_client,
            budget_allocator=budget_allocator,
            max_loops_safety_cap=max_loops,
            spec_check_synthesizer=spec_synthesizer,
            max_global_no_progress_loops=(
                ctx.repo.get_hunger_policy(task_id).max_global_no_progress_loops
            ),
        )
        orchestrator.workspace_manager.ensure_task_workspace(task_id)

        report = asyncio.run(orchestrator.run(task_id))
        skill_card = SkillManager(ctx.repo).maybe_create_skill_card(task_id, report)
        # Persist the DONE stop report BEFORE auto-promotion so candidates
        # depending on non_volatile can promote during the same completed
        # run (VAL-MEM-007).
        ctx.repo.save_stop_report(report)
        if report.stop_reason.value == "done":
            from hungerloop.services.memory_manager import MemoryManager

            MemoryManager(ctx.repo).auto_promote(task_id)
    finally:
        # Release in finally so a crashed orchestrator doesn't leave the
        # lock held. SQLiteRepository will move this into the same
        # transaction as save_stop_report; InMemory has no transaction
        # boundary, so finally is the closest equivalent.
        ctx.repo.release_task_lock(task_id, owner)

    click.echo(f"Task {task_id} stopped: {report.stop_reason.value}")
    if skill_card is not None:
        click.echo(f"  skill_card_candidate: {skill_card.skill_candidate_id}")
    click.echo(f"  goal_status: {report.goal_status}")
    click.echo(f"  total_loops: {report.total_loops}")
    click.echo(f"  total_cost_usd: {report.total_cost_usd:.4f}")
    click.echo(f"  total_tokens: {report.total_tokens}")
    if report.recommendation:
        click.echo(f"  recommendation: {report.recommendation}")


def _apply_user_overrides(
    ctx: CliContext,
    task_id: str,
    *,
    refill_loops: int | None,
    budget_loops: int | None,
    spend_budget: bool,
    refinement_profile: str | None,
    max_refinement_tier: int,
    ignore_stagnation: bool,
    draft_k: int | None,
    unblock_all: bool,
    resume_human: bool,
    raise_cost_ceiling: float | None,
) -> None:
    """Translate CLI flags into repository state mutations.

    Done up-front so the same operation is observable both via
    ``hungerloop hunger ...`` and via the corresponding ``run`` flag.
    Side-effects:

    * ``--refill N`` → ``clock.loop_count = max(0, loop_count - N)`` and
      an audit event (PRD §28.12).
    * ``--unblock-all`` → every BLOCKED item flipped back to OPEN with
      counters reset, plus a per-item audit event (PRD §15.2).
    * ``--raise-cost-ceiling X`` → ``policy.max_total_cost_usd = X``.
    * ``--draft-k N`` → ``policy.draft_sampling_k = N``.
    """
    if (
        budget_loops is not None
        or spend_budget
        or refinement_profile is not None
        or max_refinement_tier > 0
        or ignore_stagnation
        or draft_k is not None
    ):
        policy = ctx.repo.get_hunger_policy(task_id)
        if budget_loops is not None:
            policy.decay_type = DecayType.LOOP_COUNT
            policy.decay_duration_seconds = float(budget_loops)
        if spend_budget:
            policy.completion_mode = CompletionMode.SPEND_BUDGET
        if refinement_profile is not None:
            policy.refinement_profile = refinement_profile
        policy.max_refinement_tier = max_refinement_tier
        if ignore_stagnation:
            policy.respect_stagnation = False
        if draft_k is not None:
            policy.draft_sampling_k = draft_k
        ctx.repo.set_hunger_policy(task_id, policy)

    if refill_loops is not None and refill_loops > 0:
        clock = ctx.repo.get_hunger_clock(task_id)
        before = clock.loop_count
        clock.loop_count = max(0, clock.loop_count - refill_loops)
        ctx.repo.save_hunger_clock(clock)
        ctx.repo.append_event(
            EventType.HUNGER_REFILLED,
            {
                "amount_loops": refill_loops,
                "before": before,
                "after": clock.loop_count,
            },
            task_id=task_id,
        )

    if unblock_all:
        ledger = ctx.repo.get_hunger_ledger(task_id)
        from hungerloop.models.enums import HungerItemStatus

        changed = False
        for item in ledger.items:
            if item.status == HungerItemStatus.BLOCKED:
                item.status = HungerItemStatus.OPEN
                item.consecutive_failure_count = 0
                item.last_progress_loop_id = None
                changed = True
                ctx.repo.append_event(
                    EventType.HUMAN_UNBLOCKED_HUNGER_ITEM,
                    {"item_id": item.id, "via": "--unblock-all"},
                    task_id=task_id,
                )
        if changed:
            ctx.repo.save_hunger_ledger(task_id, ledger)
        ctx.repo.reset_no_progress_streak(task_id)

    if resume_human:
        clock = ctx.repo.get_hunger_clock(task_id)
        if clock.frozen:
            clock.frozen = False
            ctx.repo.save_hunger_clock(clock)
            ctx.repo.append_event(
                EventType.HUNGER_RESUMED,
                {"via": "run --resume"},
                task_id=task_id,
            )

    if raise_cost_ceiling is not None:
        policy = ctx.repo.get_hunger_policy(task_id)
        policy.max_total_cost_usd = raise_cost_ceiling
        # InMemoryRepository's set_hunger_policy is the helper; protocol-only
        # callers will reach a SQLiteRepository setter when v0.5b ships.
        if hasattr(ctx.repo, "set_hunger_policy"):
            ctx.repo.set_hunger_policy(task_id, policy)
        ctx.repo.append_event(
            EventType.COST_CEILING_RAISED,
            {"new_ceiling_usd": raise_cost_ceiling},
            task_id=task_id,
        )


def _validate_budgeted_refinement_flags(
    *,
    max_loops: int,
    budget_loops: int | None,
    spend_budget: bool,
    refinement_profile: str | None,
    max_refinement_tier: int,
    ignore_stagnation: bool,
) -> None:
    if spend_budget and budget_loops is None:
        raise click.UsageError("--spend-budget requires --budget-loops.")
    if spend_budget and max_refinement_tier <= 0:
        raise click.UsageError(
            "--spend-budget requires --max-refinement-tier greater than 0."
        )
    if spend_budget and refinement_profile is None:
        # Without a profile, RefinementCompiler.ensure_next_tier returns
        # exhausted=True at the unsupported-profile branch and the
        # orchestrator falls back to STOP_ON_DONE behavior. The user paid
        # the activation cost (--spend-budget --budget-loops --max-tier)
        # and would silently get DONE on tier 0 — fail loudly instead.
        raise click.UsageError(
            "--spend-budget requires --refinement-profile "
            "(currently 'python_medium' is the only built-in)."
        )
    if ignore_stagnation and not spend_budget:
        raise click.UsageError("--ignore-stagnation requires --spend-budget.")
    if budget_loops is not None and budget_loops <= 0:
        raise click.UsageError("--budget-loops must be a positive integer.")
    if max_refinement_tier < 0:
        raise click.UsageError("--max-refinement-tier must be >= 0.")
    if budget_loops is not None and max_loops < budget_loops:
        raise click.UsageError("--max-loops must be >= --budget-loops.")


def _build_spec_check_synthesizer(
    *,
    repo: RepositoryProtocol,
    task_id: str,
) -> _SpecSynthesizerProtocol | None:
    """Build a :class:`SpecCheckSynthesizer` for the production run path.

    When ``synthesis_enabled`` is ``False`` (the default), returns
    ``None`` without constructing a model client, reading credentials,
    or performing any synthesis mutation.

    When ``synthesis_enabled`` is ``True``, constructs a real completion
    client from ``.env`` credentials, builds the synthesizer with the
    configured ``model_name``, and returns it so
    :func:`build_orchestrator` can wire it into the orchestrator for
    post-commit synthesis.
    """
    from hungerloop.models.hunger import HungerPolicy

    policy = repo.get_hunger_policy(task_id)
    if not isinstance(policy, HungerPolicy):
        return None
    if not policy.synthesis_enabled:
        return None
    if policy.synthesis_max_total_items <= 0:
        return None

    # Build the completion client from env credentials.
    # _build_synthesis_completion_client expects a CliContext but only
    # accesses .repo; we pass a minimal stub since we already have repo.
    api_key = os.environ.get("HUNGERLOOP_API_KEY")
    base_url = os.environ.get("HUNGERLOOP_BASE_URL")
    if not api_key or not base_url:
        return None

    from hungerloop.cli.mission_cmd import (
        _build_synthesis_completion_client,
        _synthesis_model_name,
    )

    ctx_stub = type("_CtxStub", (), {"repo": repo})()
    synthesis_model = _synthesis_model_name()
    completion_client = _build_synthesis_completion_client(
        ctx_stub,
        model_name=synthesis_model,
    )
    if completion_client is None:
        return None

    from hungerloop.services.check_proposal_gate import CheckProposalGate, SandboxDryRunner
    from hungerloop.services.cost_guard import CostGuard
    from hungerloop.services.sandbox_runner import SandboxRunner
    from hungerloop.services.spec_check_synthesizer import SpecCheckSynthesizer

    return SpecCheckSynthesizer(
        repo=repo,
        cost_guard=CostGuard(repo),
        completion_client=completion_client,
        gate=CheckProposalGate(
            dry_runner=SandboxDryRunner(SandboxRunner(repo)),
        ),
        model_name=synthesis_model,
    )


def _resolve_model_client(
    ctx: CliContext,
    model_config_path: Path | None,
    *,
    accept_unknown_pricing: bool = False,
) -> ModelClient | None:
    if model_config_path is None:
        return ctx.model_client

    config = ModelConfigLoader().load(model_config_path)
    if config.provider == ModelProvider.DUMMY:
        return DummyModelClient()
    if config.provider == ModelProvider.OPENAI:
        if config.model_name not in PricingTable.PRICES:
            ctx.repo.append_event(
                EventType.UNKNOWN_MODEL_PRICING,
                {
                    "model": config.model_name,
                    "source": "model_config",
                    "accepted_by_flag": accept_unknown_pricing,
                },
            )
        return OpenAIModelClient(
            config,
            CostGuard(ctx.repo),
            PricingTable(ctx.repo),
            ctx.repo,
        )
    raise NotImplementedError(f"Unsupported provider: {config.provider.value}")


def _resolve_budget_allocator(model_config_path: Path | None) -> BudgetAllocator:
    """Honor YAML model max_tokens for real-model runs.

    The default CLI wiring used the stock 4000-token explore/exploit caps even
    when the operator explicitly configured a larger model ``max_tokens`` in
    YAML. That left long-code tasks prone to truncated JSON responses. Keep the
    default behavior for runs without a model config, but let an OpenAI-backed
    config raise the per-loop request budget.

    NOTE: ``config.max_tokens`` is the **per-call response cap** — the
    deepseek/OpenAI ``max_tokens`` request parameter. The
    ``BudgetAllocator`` allocates a **per-assignment cumulative budget**
    that the worker spends across MAX_SELF_REPAIR_ITERATIONS+1 inner-loop
    calls. Using the same value for both clamps a 6-call inner-loop to a
    single response and trips ``worker_budget_exceeded`` after the first
    reasoning-heavy call. Pre-multiply by an inner-loop headroom factor
    so the cumulative cap actually accommodates the inner-loop.
    """
    if model_config_path is None:
        return BudgetAllocator()

    config = ModelConfigLoader().load(model_config_path)
    if config.provider != ModelProvider.OPENAI:
        return BudgetAllocator()

    # MAX_SELF_REPAIR_ITERATIONS + 1 outer-call budget, with a small
    # safety pad for input-side accumulation across iterations. The
    # constant lives in execution_worker; import here would create a
    # cycle, so reference the same numeric value.
    inner_loop_headroom = 6 * 2
    cumulative_cap = max(60_000, config.max_tokens * inner_loop_headroom)
    return BudgetAllocator(
        explore_max_tokens=cumulative_cap,
        exploit_max_tokens=cumulative_cap,
    )
