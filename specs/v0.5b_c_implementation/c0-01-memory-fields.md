# c0-01 · MemoryCandidate forward-compat fields

**Spec**: §5. **PRD**: §19.1. **Release**: v0.5c.0.

## Goal

Add the four lifecycle fields to `MemoryCandidate` *now* (v0.5c) so v0.6 promotion doesn't require a breaking schema migration. v0.5c only emits `state="proposed"`; everything else stays null/default.

## Files to touch

- `src/hungerloop/models/memory.py` — extend `MemoryCandidate`.
- `src/hungerloop/services/memory_manager.py` — set `state="proposed"` explicitly.
- `src/hungerloop/repository/migrations/v2__memory_candidate_lifecycle.sql` (new — drives `LATEST_VERSION` to 2 in `b0-02`'s migrator).
- `src/hungerloop/cli/memory_cmd.py` — add `--state` filter on `memory list`.
- `tests/unit/test_memory_manager.py` (extend).
- **NEW** `tests/unit/test_memory_state_filter.py`.

## Checklist

### Model (`memory.py`)

- [ ] Add to `MemoryCandidate`:

```python
state: Literal["proposed", "approved", "rejected", "expired", "superseded"] = "proposed"
decision_loop_id: int | None = None
decided_by: Literal["human", "auto", None] = None
decision_rationale: str = ""
replaces_candidate_id: str | None = None
expires_at: datetime | None = None  # 90-day default applied at emit time
```

- [ ] On creation in `MemoryManager.propose_from_loop`: set `expires_at = created_at + timedelta(days=90)`. (No auto-job acts on this in v0.5c — pure data.)

### Manager (`memory_manager.py`)

- [ ] Make `state="proposed"` explicit at the construction site (don't rely on the default).
- [ ] Compute and set `expires_at` per the 90-day rule.
- [ ] Do NOT write any other state value in v0.5c. Add a comment: `# v0.5c: only "proposed" is emitted. Promotion to "approved"/"rejected"/etc. lands in v0.6.`

### Migration (`v2__memory_candidate_lifecycle.sql`)

- [ ] `ALTER TABLE memory_candidates ADD COLUMN state TEXT NOT NULL DEFAULT 'proposed'`
- [ ] `ADD COLUMN decision_loop_id INTEGER`
- [ ] `ADD COLUMN decided_by TEXT`
- [ ] `ADD COLUMN decision_rationale TEXT NOT NULL DEFAULT ''`
- [ ] `ADD COLUMN replaces_candidate_id TEXT`
- [ ] `ADD COLUMN expires_at TEXT`  -- ISO8601 UTC, nullable
- [ ] `CREATE INDEX idx_memory_state ON memory_candidates(task_id, state)`
- [ ] Final line: `PRAGMA user_version = 2;`
- [ ] Bump `LATEST_VERSION = 2` in `repository/sqlite_migrator.py`.

### CLI (`memory_cmd.py`)

- [ ] Add `@click.option("--state", type=click.Choice(["proposed","approved","rejected","expired","superseded","all"]), default="all")`.
- [ ] Filter results in Python (the InMemory repo doesn't have an indexed query) — fine for v0.5c; SQLite path uses the new index.

## Tests

### Extend `test_memory_manager.py`

- [ ] `test_emitted_candidate_has_state_proposed`
- [ ] `test_emitted_candidate_has_expires_at_90_days_out` (use frozen time helper)
- [ ] `test_decision_fields_default_null` — `decision_loop_id`, `decided_by`, `decision_rationale==""`, `replaces_candidate_id is None`.

### New `test_memory_state_filter.py`

- [ ] `test_memory_list_default_returns_all_states`
- [ ] `test_memory_list_state_proposed_filters_correctly` — synthesize one approved candidate manually, confirm filter excludes it.
- [ ] `test_memory_list_state_all_includes_everything`

## Done when

- [ ] All 6 tests pass (3 extend + 3 new).
- [ ] `mypy --strict` clean.
- [ ] Migration v2 applies cleanly on a v1 DB; `user_version` advances to 2.
- [ ] PRD §19.1 references the new fields and their v0.5c-emit-only constraint.

## Notes

- Even though v0.5c doesn't *act* on `expires_at`, writing it now means v0.6's auto-expiry job can be a pure read-and-flip without a schema migration.
- `replaces_candidate_id` is a forward-pointer (new candidate points at the one it replaces). One-way chain — see spec §5.4.
- Keep `MemoryCandidate` frozen-by-default if that's the v0.5a convention — only `state` is mutable in v0.6, and that mutation happens via repo writes (model itself stays a snapshot).
