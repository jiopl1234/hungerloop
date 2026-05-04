"""Perf budget for ``hungerloop report`` and ``hungerloop status`` (PRD §9 NFR).

Opt-in suite — excluded from the default ``pytest`` run via the
``testpaths``/marker config in ``pyproject.toml``. Run with::

    pytest tests/perf -m perf

The 200 ms target is for warm-cache reads against an in-process
``InMemoryRepository`` carrying ~100 loops of state. Cold-start (process
spawn + DB open + lock acquire) is excluded — that path is dominated
by interpreter startup, not query time.

CI runners can be slower than a developer laptop; tighten or relax the
budget there via ``HUNGERLOOP_PERF_BUDGET_MS`` if a regression keeps
flapping the run.
"""
from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import HungerItemStatus, LoopPhase
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.tracing import LoopTrace
from hungerloop.repository.in_memory_repo import InMemoryRepository

_DEFAULT_BUDGET_MS = 200.0


def _budget_ms() -> float:
    raw = os.environ.get("HUNGERLOOP_PERF_BUDGET_MS")
    if not raw:
        return _DEFAULT_BUDGET_MS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_MS


@pytest.fixture
def loaded_context(tmp_path: Path) -> CliContext:
    """Synthesize a fixture task with 100 loops + 100 evidence rows + 5
    best-state promotions so ``report``/``status`` exercise non-trivial
    aggregation paths.
    """
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "perf-1",
        HungerPolicy(
            max_total_cost_usd=100.0,
            max_total_tokens=10_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=60.0,
        ),
    )
    items = [
        HungerItem(
            id=f"H-{i:03d}",
            title=f"check {i}",
            gap_score=0.0,
            status=HungerItemStatus.VALIDATED_SATISFIED,
        )
        for i in range(20)
    ]
    repo.save_hunger_ledger(
        "perf-1", HungerLedger(task_id="perf-1", items=items)
    )
    for item in items:
        repo.save_hunger_item(item)
    for loop_id in range(1, 101):
        repo.save_loop_trace(
            LoopTrace(
                task_id="perf-1",
                loop_id=loop_id,
                phase=LoopPhase.EXPLORE.value,
                active_hunger=80.0,
                drive_budget=80.0,
                work_pressure=10.0,
                committed=loop_id % 2 == 0,
                delta_summary=f"loop {loop_id}",
                next_action="continue",
            )
        )
        repo.save_shell_output_as_evidence(
            task_id="perf-1",
            loop_id=loop_id,
            label=f"run-{loop_id}",
            argv=["echo", "ok"],
            cwd="/",
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
        )
    for promote_id in range(1, 6):
        repo.save_best_state(
            BestState(
                task_id="perf-1",
                state_id=f"STATE-perf-1-{promote_id}",
                summary=f"promotion {promote_id}",
                accepted_check_keys=[f"H-{i:03d}:0" for i in range(promote_id)],
            )
        )
    return CliContext(repo=repo, workspace_root=tmp_path)


def _measure_ms(invoke: Callable[[], object]) -> float:
    start = time.perf_counter()
    invoke()
    return (time.perf_counter() - start) * 1000.0


@pytest.mark.perf
def test_report_under_200ms(loaded_context: CliContext) -> None:
    runner = CliRunner()

    def _run() -> None:
        result = runner.invoke(cli, ["report", "perf-1"], obj=loaded_context)
        assert result.exit_code == 0

    timings = [_measure_ms(_run) for _ in range(5)]
    median = statistics.median(timings)
    budget = _budget_ms()
    assert median < budget, (
        f"hungerloop report median {median:.1f} ms exceeds {budget:.1f} ms; "
        f"timings={[round(t, 1) for t in timings]}"
    )


@pytest.mark.perf
def test_status_under_200ms(loaded_context: CliContext) -> None:
    runner = CliRunner()

    def _run() -> None:
        result = runner.invoke(cli, ["status", "perf-1"], obj=loaded_context)
        assert result.exit_code == 0

    timings = [_measure_ms(_run) for _ in range(5)]
    median = statistics.median(timings)
    budget = _budget_ms()
    assert median < budget, (
        f"hungerloop status median {median:.1f} ms exceeds {budget:.1f} ms; "
        f"timings={[round(t, 1) for t in timings]}"
    )
