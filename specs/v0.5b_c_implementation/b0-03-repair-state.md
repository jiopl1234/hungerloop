# b0-03 · `hungerloop repair-state` — `--check` + safe `--fix`

**Spec**: §3. **PRD**: §16.3. **Release**: v0.5b.0 (`--check` mandatory; `--fix` for D4/D5 only this release).

## Goal

Detect divergence between SQLite state and the workspace, with strict policy: filesystem is truth, SQLite is auxiliary index. Never delete files; never overwrite `best/`.

## Divergence catalog

| ID | Condition | `--check` | `--fix` |
|---|---|---|---|
| D1 | manifest matches file hash | OK | nothing |
| D2 | manifest hash mismatch | **CORRUPTION** | refuse — exit 2 |
| D3 | manifest entry, file missing | **CORRUPTION** | refuse — exit 2 |
| D4 | file present, no manifest | warn | rewrite manifest from filesystem |
| D5 | orphan candidate workspace (no LoopTrace, no candidate row) | warn | move to `rejected/loop_NNN/` |
| D6 | stale task lock (`now - locked_at > stale_threshold`) | warn | refuse — direct user to `--steal-lock` |
| D7 | `accepted_checks` row references missing `validation_id` | warn | refuse (defer to v0.5b.1) |

## Files to touch

- **NEW** `src/hungerloop/services/repair_state.py` — `RepairStateService` with `detect()` (read-only) and `apply_fix()`.
- **NEW** `src/hungerloop/cli/repair_state_cmd.py` — click command.
- `src/hungerloop/cli/main.py` — register.
- **NEW** `tests/unit/test_repair_state.py`.
- (`workspace_manifest` model already exists at `models/workspace.py`; reuse.)

## Checklist

### Service (`repair_state.py`)

- [ ] `class Divergence(BaseModel)`: `kind: Literal["D1"..."D7"]`, `target: str` (file path or candidate dir), `detail: str`, `corruption: bool`.
- [ ] `detect(task_id) -> list[Divergence]` — pure read; touches SQLite + filesystem; emits Divergence rows.
- [ ] `apply_fix(divergence) -> tuple[bool, str]` — returns `(fixed, summary)`. Refuses on `corruption=True` or D6/D7. Writes a `repair_state_action` event for every action attempted (including refusals).
- [ ] D4 fix: rebuild `WorkspaceManifest` for `best/` from current file hashes; write to repo.
- [ ] D5 fix: `WorkspaceManager.reject_candidate(task_id, loop_id)` (existing API).

### CLI (`repair_state_cmd.py`)

- [ ] `@click.command("repair-state")`, `@click.argument("task_id")`, mutually-exclusive `--check / --fix`.
- [ ] `--check` exit codes: 0 clean, 1 warnings (D4/D5/D6/D7), 2 corruption (D2/D3).
- [ ] `--fix` exit codes: 0 all repairable fixed, 2 corruption present, 3 nothing to fix.
- [ ] Always prints a one-line-per-divergence summary to stdout.

### Tests (`test_repair_state.py`)

- [ ] `test_detect_d1_clean_state_returns_empty`
- [ ] `test_detect_d2_hash_mismatch_marks_corruption`
- [ ] `test_detect_d3_missing_file_marks_corruption`
- [ ] `test_detect_d4_missing_manifest_warns_only`
- [ ] `test_detect_d5_orphan_candidate_warns_only`
- [ ] `test_detect_d6_stale_lock_warns_only`
- [ ] `test_fix_d4_rewrites_manifest_and_emits_event`
- [ ] `test_fix_d5_moves_orphan_to_rejected`
- [ ] `test_fix_d2_refuses_with_exit_2`
- [ ] `test_fix_d6_refuses_and_directs_to_steal_lock`
- [ ] `test_fix_never_deletes_or_overwrites_files_in_best` — snapshot best/ before, run all fix paths, assert byte-equal after.

## Done when

- [ ] All 11 tests pass.
- [ ] `mypy --strict` clean.
- [ ] `hungerloop repair-state demo-1 --check` against the in-memory demo prints "clean" and exits 0.
- [ ] PRD §16.3 references this implementation.

## Notes

- D7 detection lands in v0.5b.0 (so ops can see it); the fix is deferred — refusing it is fine for now.
- The `repair_state_action` event uses the new `EventType.REPAIR_STATE_ACTION` from `b0-05`. If `b0-05` lands later, fall back to a literal string and migrate when the enum lands.
