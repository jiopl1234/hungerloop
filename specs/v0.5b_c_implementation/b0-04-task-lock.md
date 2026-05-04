# b0-04 · Task lock fault recovery

**Spec**: §4. **PRD**: §5.1.1. **Release**: v0.5b.0.

## Goal

Detect stale locks, allow explicit steal, atomic release on clean shutdown. Re-entrant from same `(hostname, pid)` succeeds without ceremony.

## Files to touch

- `src/hungerloop/repository/protocol.py` — already declares `acquire_task_lock / release_task_lock`. Add `steal_task_lock(task_id, new_owner) -> dict[str, str]` (returns prev_owner + prev_locked_at).
- `src/hungerloop/repository/in_memory_repo.py` — implement.
- `src/hungerloop/repository/sqlite_repo.py` (parallel task) — implement using `BEGIN IMMEDIATE`.
- `src/hungerloop/cli/run_cmd.py` — add `--steal-lock`, `--lock-stale-sec N` flags; lock-acquire path on entry.
- `src/hungerloop/services/loop_orchestrator.py` — release lock in same transaction as `save_stop_report` (the v0.5b.0 D1-B migration already moves StopReport persistence here, see `hungerloop_v0_5b_c_prd.md` §1.1.1).
- **NEW** `tests/unit/test_task_lock.py`.

## Checklist

### Owner string

- [ ] Format: `f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"`.
- [ ] Built once per CLI invocation in `run_cmd.py`; passed to repo.

### Lock acquisition flow (in `run_cmd.py`)

- [ ] On `hungerloop run <task_id>`:
  1. Compute `stale_threshold` = `--lock-stale-sec` flag if given else `int(os.environ.get("HUNGERLOOP_LOCK_STALE_SEC", "1800"))` (= 30 min).
  2. Build `owner` string.
  3. Call `repo.acquire_task_lock(task_id, owner, stale_threshold_seconds=stale_threshold, steal=args.steal_lock)`.
  4. Repo returns one of: `"acquired"`, `"reentrant"`, `"held_live"`, `"held_stale"`, `"stolen"`.
  5. Map to exit codes: `held_live → 3`, `held_stale (without --steal-lock) → 6`, others → proceed.

### Repo behavior (`in_memory_repo.py` and the future `sqlite_repo.py`)

- [ ] `acquire_task_lock(task_id, owner, *, stale_threshold_seconds, steal=False) -> str`:
  - No prior owner → set, return `"acquired"`.
  - Prior owner == new owner (same host+pid) → return `"reentrant"`.
  - Prior owner held live (delta < stale_threshold) → return `"held_live"` (do NOT mutate).
  - Prior owner held stale, `steal=False` → return `"held_stale"` (do NOT mutate).
  - Prior owner held, `steal=True` → record prev, replace, append `lock_stolen` event with `{prev_owner, prev_locked_at, new_owner}`, return `"stolen"`.
- [ ] `release_task_lock(task_id, owner)`: only releases if `owner` matches; no-op otherwise (defends against double-release after steal).

### Atomic release on clean shutdown

- [ ] `LoopOrchestrator._emit_stop` (or whatever the §12.0 pipeline ends up calling): when persisting StopReport, also call `release_task_lock(task_id, owner)` inside the same `repo.transaction()` block.
- [ ] On crash: lock stays held. The next `run` either re-enters (same pid — won't normally happen post-crash), or sees the stale lock after `stale_threshold` elapses, or requires `--steal-lock`.

### Tests (`test_task_lock.py`)

- [ ] `test_first_run_acquires_lock`
- [ ] `test_reentrant_same_owner_succeeds`
- [ ] `test_held_live_lock_blocks_with_exit_3`
- [ ] `test_held_stale_lock_without_steal_exits_6`
- [ ] `test_steal_lock_replaces_owner_and_emits_event`
- [ ] `test_clean_shutdown_releases_lock_atomically_with_stop_report`
- [ ] `test_release_only_releases_own_lock` — owner B can't release owner A's lock.
- [ ] `test_lock_stale_sec_cli_overrides_env`

## Done when

- [ ] All 8 tests pass.
- [ ] `mypy --strict` clean.
- [ ] PRD §5.1.1 references this implementation.
- [ ] `repair-state --check` (b0-03) reports D6 when a stale lock is present.

## Notes

- `--steal-lock` is intentionally explicit; tooling that wraps `hungerloop run` (CI) should NEVER pass it implicitly.
- The `stolen` event payload feeds future "who stole the lock" forensics. Don't drop fields.
