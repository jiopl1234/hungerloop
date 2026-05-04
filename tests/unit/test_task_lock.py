"""Task lock fault-recovery tests (PRD §5.1.1).

Covers the acquire/release/steal contract on InMemoryRepository plus the
CLI integration of `--steal-lock` and `--lock-stale-sec`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.cli.run_cmd import (
    DEFAULT_LOCK_STALE_SEC,
    _build_lock_owner,
    _resolve_stale_threshold,
)
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerLedger, HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository

# ---------------------------------------------------------------------------
# Repository-level acquire / release / steal
# ---------------------------------------------------------------------------


def test_first_run_acquires_lock() -> None:
    repo = InMemoryRepository()
    outcome = repo.acquire_task_lock(
        "t1", "host:42:abcd1234", stale_threshold_seconds=1800
    )
    assert outcome == "acquired"
    assert repo._task_locks["t1"]["owner"] == "host:42:abcd1234"


def test_reentrant_same_host_pid_succeeds() -> None:
    """Same hostname:pid (uuid suffix differs) is treated as the same
    physical process — pass-through, no mutation."""
    repo = InMemoryRepository()
    repo.acquire_task_lock(
        "t1", "host:42:first", stale_threshold_seconds=1800
    )
    locked_at_before = repo._task_locks["t1"]["locked_at"]

    outcome = repo.acquire_task_lock(
        "t1", "host:42:second", stale_threshold_seconds=1800
    )
    assert outcome == "reentrant"
    # Owner string is NOT replaced — proves no mutation.
    assert repo._task_locks["t1"]["owner"] == "host:42:first"
    assert repo._task_locks["t1"]["locked_at"] == locked_at_before


def test_held_live_lock_blocks_other_process() -> None:
    repo = InMemoryRepository()
    repo.acquire_task_lock(
        "t1", "host-a:42:aaaa", stale_threshold_seconds=1800
    )
    outcome = repo.acquire_task_lock(
        "t1", "host-b:99:bbbb", stale_threshold_seconds=1800
    )
    assert outcome == "held_live"
    # Original owner unchanged.
    assert repo._task_locks["t1"]["owner"] == "host-a:42:aaaa"


def test_held_stale_lock_without_steal_returns_held_stale() -> None:
    repo = InMemoryRepository()
    # Manually plant a stale lock so we don't have to wait 30 minutes.
    repo._task_locks["t1"] = {
        "owner": "host-a:42:aaaa",
        "locked_at": datetime.now(timezone.utc) - timedelta(seconds=3600),
    }
    outcome = repo.acquire_task_lock(
        "t1", "host-b:99:bbbb", stale_threshold_seconds=1800
    )
    assert outcome == "held_stale"
    # No mutation without --steal-lock.
    assert repo._task_locks["t1"]["owner"] == "host-a:42:aaaa"


def test_steal_lock_replaces_owner_and_emits_event() -> None:
    repo = InMemoryRepository()
    prev_locked_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    repo._task_locks["t1"] = {
        "owner": "host-a:42:aaaa",
        "locked_at": prev_locked_at,
    }
    outcome = repo.acquire_task_lock(
        "t1",
        "host-b:99:bbbb",
        stale_threshold_seconds=1800,
        steal=True,
    )
    assert outcome == "stolen"
    assert repo._task_locks["t1"]["owner"] == "host-b:99:bbbb"
    # Audit event written.
    stolen = [
        e
        for e in repo._events
        if e["event_type"] == EventType.LOCK_STOLEN.value
    ]
    assert len(stolen) == 1
    payload = stolen[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["prev_owner"] == "host-a:42:aaaa"
    assert payload["new_owner"] == "host-b:99:bbbb"
    # Z-suffix wire shape (matches events.created_at).
    assert payload["prev_locked_at"] == (
        prev_locked_at.isoformat().replace("+00:00", "Z")
    )


def test_steal_works_on_live_lock_too() -> None:
    """``--steal-lock`` is operator-grade force; it shouldn't refuse just
    because the previous owner was technically still inside the threshold."""
    repo = InMemoryRepository()
    repo.acquire_task_lock(
        "t1", "host-a:42:aaaa", stale_threshold_seconds=1800
    )
    outcome = repo.acquire_task_lock(
        "t1",
        "host-b:99:bbbb",
        stale_threshold_seconds=1800,
        steal=True,
    )
    assert outcome == "stolen"


def test_release_only_releases_own_lock() -> None:
    """Owner B cannot release A's lock — defends against double-release
    after a steal."""
    repo = InMemoryRepository()
    repo.acquire_task_lock(
        "t1", "host-a:42:aaaa", stale_threshold_seconds=1800
    )
    repo.release_task_lock("t1", "host-b:99:bbbb")
    assert "t1" in repo._task_locks  # A still owns it
    repo.release_task_lock("t1", "host-a:42:aaaa")
    assert "t1" not in repo._task_locks


def test_release_unknown_task_is_noop() -> None:
    repo = InMemoryRepository()
    repo.release_task_lock("never-locked", "anyone")  # no error


# ---------------------------------------------------------------------------
# Helper: stale-threshold resolution
# ---------------------------------------------------------------------------


def test_resolve_stale_threshold_default() -> None:
    assert _resolve_stale_threshold(None) == DEFAULT_LOCK_STALE_SEC


def test_resolve_stale_threshold_env_overrides_default(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HUNGERLOOP_LOCK_STALE_SEC", "120")
    assert _resolve_stale_threshold(None) == 120


def test_resolve_stale_threshold_cli_overrides_env(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HUNGERLOOP_LOCK_STALE_SEC", "120")
    assert _resolve_stale_threshold(60) == 60


def test_resolve_stale_threshold_garbage_env_falls_back(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HUNGERLOOP_LOCK_STALE_SEC", "not-a-number")
    assert _resolve_stale_threshold(None) == DEFAULT_LOCK_STALE_SEC


def test_build_lock_owner_format() -> None:
    owner = _build_lock_owner()
    parts = owner.split(":")
    assert len(parts) == 3
    # hostname:pid:uuid8 — uuid is hex chars, length 8
    assert parts[1].isdigit()
    assert len(parts[2]) == 8


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> CliContext:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.get_hunger_clock("t1")
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[]))
    return CliContext(repo=repo, workspace_root=tmp_path)


def test_cli_run_held_live_exits_3(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Plant a lock from a different host:pid — no `is`-equality magic, just
    # a different prefix so reentrant doesn't kick in.
    ctx.repo.acquire_task_lock(
        "t1", "other-host:1:zzzz", stale_threshold_seconds=1800
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=ctx)
    assert result.exit_code == 3
    assert "locked by another live process" in result.output


def test_cli_run_held_stale_exits_6(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.repo._task_locks["t1"] = {
        "owner": "other-host:1:zzzz",
        "locked_at": datetime.now(timezone.utc) - timedelta(seconds=3600),
    }
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=ctx)
    assert result.exit_code == 6
    assert "stale lock" in result.output
    assert "--steal-lock" in result.output


def test_cli_run_steal_lock_proceeds(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.repo._task_locks["t1"] = {
        "owner": "other-host:1:zzzz",
        "locked_at": datetime.now(timezone.utc) - timedelta(seconds=3600),
    }
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1", "--steal-lock"], obj=ctx)
    assert result.exit_code == 0, result.output
    # Lock_stolen event present.
    stolen = [
        e
        for e in ctx.repo._events
        if e["event_type"] == EventType.LOCK_STOLEN.value
    ]
    assert len(stolen) == 1


def test_cli_run_releases_lock_on_clean_shutdown(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=ctx)
    assert result.exit_code == 0, result.output
    # Lock has been released; another caller can acquire freshly.
    assert "t1" not in ctx.repo._task_locks


def test_cli_run_releases_lock_on_orchestrator_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the orchestrator must NOT leave the lock held —
    the ``finally`` clause in ``run_cmd.run`` is the only thing that
    keeps a stuck task from looking like a stale-lock situation forever.
    """
    ctx = _ctx(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic orchestrator failure")

    # Replace the orchestrator factory so ``run`` raises after the lock
    # is acquired but before any clean shutdown path runs.
    import hungerloop.cli.run_cmd as run_cmd

    monkeypatch.setattr(run_cmd, "build_orchestrator", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "t1"], obj=ctx)
    assert result.exit_code != 0
    # The lock must still be released even though the orchestrator blew up.
    assert "t1" not in ctx.repo._task_locks


def test_cli_lock_stale_sec_flag_overrides_threshold(tmp_path: Path) -> None:
    """With `--lock-stale-sec 1`, a 5-second-old lock is already stale."""
    ctx = _ctx(tmp_path)
    ctx.repo._task_locks["t1"] = {
        "owner": "other-host:1:zzzz",
        "locked_at": datetime.now(timezone.utc) - timedelta(seconds=5),
    }
    runner = CliRunner()
    # Default 30-min threshold would say "live"; --lock-stale-sec 1 says stale.
    result = runner.invoke(cli, ["run", "t1", "--lock-stale-sec", "1"], obj=ctx)
    assert result.exit_code == 6  # held_stale, not held_live
