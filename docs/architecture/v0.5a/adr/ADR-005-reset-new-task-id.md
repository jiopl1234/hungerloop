# ADR-005: `--reset` creates a new task_id; no generation column

## Status
Accepted (2026-05-02)

## Context

PRD §18.2 / §28.13 (M14 + M17) requires `hungerloop run <task_id> --reset` to start a task fresh while preserving the original task's data for inspection.

The schema-level question: how do we represent the same logical task across resets?

Two structural options:

- (a) Composite primary keys everywhere: `(task_id, generation INTEGER)` on every table.
- (b) New `task_id` per reset: `<original>__r1`, `<original>__r2`, etc. Each is a fully independent row across all tables.

Per ADR-001, persistence is one SQLite DB per task — so (b) also means a new SQLite *file* per reset.

## Decision

**`--reset` mints a new task_id of the form `<original>__r<N>`** where N is the smallest integer such that `<original>__r<N>` does not already exist on disk.

- New task_id → new directory `workspace/tasks/<original>__r<N>/`
- New `blackboard.sqlite` file (no migration, no shared rows)
- All persistence keyed by new task_id
- `events` table payload includes `parent_task_id` field for traceability
- `hungerloop status <original>` continues to show original task's terminal state
- `hungerloop status <original>__r1` shows the reset task's state
- CLI prints the new task_id and exits, requiring user to re-run `hungerloop run <new_task_id>` if they want to start it

## Alternatives Considered

### A. `generation INTEGER NOT NULL DEFAULT 1` on every table
Composite PKs: `(task_id, generation)` everywhere. `--reset` increments generation and inserts new rows.
- **Rejected** — refactors every existing PK in the schema (PRD §17.2 has 16 tables). Every Repository method gains an implicit "current generation" parameter or filter. High change cost; high bug surface (any forgotten generation filter leaks data across resets).

### B. Drop & recreate
`--reset` deletes the SQLite file and recreates schema.
- **Rejected** — destroys history, prevents post-mortem on why the user reset.

### C. Archive table per entity
`tasks_archive`, `loop_traces_archive`, etc. `--reset` copies live rows to archive, then truncates.
- **Rejected** — doubles the schema; archival logic must keep pace with every new table; query-time UNION across live + archive is ugly.

### D. Single rolling task_id with `reset_count` column
Same task_id; rows tagged with `reset_count`. Latest wins; older resets queryable.
- **Rejected** — same drawback as (A): every read must filter on `reset_count = current_reset_count`.

### E. Soft-delete via `valid_at` timestamps
Bitemporal: `valid_from`, `valid_to` on every row. `--reset` closes current rows.
- **Rejected** — bitemporal modeling is overkill for v0.5a; no analytics query benefits from it yet.

## Consequences

**Positive**
- Zero schema change to support `--reset`. Existing `task_id PRIMARY KEY` (single column) on most tables stays.
- Original task data is untouched and remains queryable forever via its original task_id.
- Workspace directories naturally segregate: `workspace/tasks/<orig>/` vs `workspace/tasks/<orig>__r1/` — no risk of file aliasing.
- `--reset` implementation is small: generate next reset id, init SQLiteRepository on new path, copy `model_config.yaml` if applicable, append `task_reset` event to original task's events table linking to the new task_id.
- ADR-001's "one SQLite per task" composes cleanly: `--reset` is just "make a new task".

**Negative**
- "Same logical task" is no longer a single key; cross-reset analytics must traverse the `__r<N>` chain via the `parent_task_id` event link.
- User-visible task_ids grow over time (`__r1`, `__r2`, ...). Acceptable: most users will reset 0–2 times per task.
- Manual collision: if a user manually creates `<orig>__r1` directory, the auto-numbering must skip it. Mitigation: scan `workspace/tasks/` for `<orig>__r*` and pick `max(N) + 1`.

## Trade-offs

Schema simplicity + history preservation > nicely-aggregated cross-reset queries. v0.5a's analytics needs are zero; we can build a `hungerloop history <task_id>` view in v0.6+ that walks the parent_task_id chain if anyone asks.

## Compliance

- `cli/run_cmd.py` `--reset` flow:
  1. require explicit `--reset` flag (no shorthand) AND interactive confirmation (`y/N`).
  2. `next_id = compute_next_reset_id(workspace_root, original_task_id)` — scans `workspace/tasks/<original>__r*`.
  3. Create `workspace/tasks/<next_id>/` directory.
  4. Initialize `SQLiteRepository(workspace/tasks/<next_id>/blackboard.sqlite)`.
  5. Reuse the original task's HungerPolicy and acceptance checks (load from original DB).
  6. Append `task_reset` event to original task's events table: `{"new_task_id": <next_id>, "reset_at": <iso>}`.
  7. Append `task_created_from_reset` event to new task's events table: `{"parent_task_id": <original>, "created_at": <iso>}`.
  8. Print new task_id; do not auto-start the run.
- `--reset` MUST NOT run if the original task's `last_stop_reason` is `None` (task still in-progress) without an additional `--abandon` flag.
- Naming pattern is exact: `<original>__r<N>` (double underscore, lowercase r, integer N starting at 1). Reserved suffix; `hungerloop new` rejects task_ids matching `__r\d+$`.
