"""Shared fixtures for the v0.5.2 integration suite.

These tests exercise the real :func:`build_orchestrator` factory, so the
fixtures here mimic what ``hungerloop run`` would build at runtime: a
seeded :class:`InMemoryRepository`, a workspace under ``tmp_path``, and
either a :class:`DummyModelClient` or the real
:class:`OpenAIModelClient` (used for the auth-error test).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.repository.in_memory_repo import InMemoryRepository

SeedFn = Callable[[InMemoryRepository], None]


def make_seed_report_task(
    *,
    initial_hunger: float = 100.0,
    decay_duration: float = 20.0,
    max_total_cost_usd: float = 10.0,
    max_total_tokens: int = 1_000_000,
) -> SeedFn:
    """Return a fixture that seeds the standard ``report.md`` task into ``repo``.

    The task has one hunger item with a single ``file_exists: report.md``
    acceptance check — the same shape as ``examples/demo_task.yaml`` but
    minimal (no ``shell_exit_zero`` so we don't depend on Python at $PATH).
    """
    def _seed(repo: InMemoryRepository) -> None:
        repo.set_hunger_policy(
            "t1",
            HungerPolicy(
                max_total_cost_usd=max_total_cost_usd,
                max_total_tokens=max_total_tokens,
                initial_hunger=initial_hunger,
                decay_duration_seconds=decay_duration,
            ),
        )
        repo.get_hunger_clock("t1")
        item = HungerItem(
            id="H-001",
            title="produce report",
            priority=1.0,
            gap_score=1.0,
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "report.md"},
                    description="report.md exists",
                ),
            ],
        )
        repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
        repo.save_hunger_item(item)

    return _seed


def make_two_check_seed() -> SeedFn:
    """Two-check task so a SkillCard can be triggered on DONE."""
    def _seed(repo: InMemoryRepository) -> None:
        repo.set_hunger_policy(
            "t1",
            HungerPolicy(
                max_total_cost_usd=10.0,
                max_total_tokens=1_000_000,
                initial_hunger=200.0,
                decay_duration_seconds=50.0,
            ),
        )
        repo.get_hunger_clock("t1")
        item = HungerItem(
            id="H-001",
            title="produce two artefacts",
            priority=1.0,
            gap_score=1.0,
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "report.md"},
                    description="report.md exists",
                ),
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "summary.md"},
                    description="summary.md exists",
                ),
            ],
        )
        repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
        repo.save_hunger_item(item)

    return _seed


def workspace(tmp_path: Path) -> Path:
    """Standard workspace path used by all integration tests."""
    return tmp_path / "workspaces"
