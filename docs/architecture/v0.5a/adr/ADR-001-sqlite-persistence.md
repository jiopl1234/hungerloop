# ADR-001: SQLite per-task as v0.5a persistence layer

## Status
Accepted (2026-05-02)

## Context

v0.4.1 ships only `InMemoryRepository`. The PRD §17 mandates a persistent backend so `hungerloop run` can resume after process restart, refill hunger, and inspect history via `hungerloop status`.

Persistence requirements:

- ACID across `clock.loop_count` advancement, `BestState` promotion, and `accepted_checks` insertion (these must all commit or none — losing partial state corrupts I-3).
- Single-machine, single-orchestrator-per-task usage (v0.5a does not need cross-host coordination).
- Zero ops cost for local dev and CI.
- ~50–500 loop_traces per task, ~100 evidence rows per loop. Volume is low.
- Read patterns: `get_*` calls are point-lookups by `task_id` or composite keys; no cross-task analytics in v0.5a.

## Decision

Use **SQLite, one file per task**, stored at `workspace/tasks/<task_id>/blackboard.sqlite`.

- Schema lives in `src/hungerloop/repository/sqlite_schema.sql` (PRD §17.2).
- `SQLiteRepository` implements `RepositoryProtocol`. WAL mode enabled (`PRAGMA journal_mode=WAL`) for crash safety.
- All multi-row state changes (commit, validation save) wrapped in a single transaction.
- Migrations are forward-only and applied by `SQLiteRepository.__init__` reading the file's `PRAGMA user_version`.

## Alternatives Considered

### A. JSONL / JSON files per entity
- Each entity (best_state, candidates, traces) in its own file under the task directory.
- **Rejected** — no atomicity across files; mid-write crash corrupts state. Recovery code would re-implement what SQLite gives for free.

### B. Single shared SQLite for all tasks (`workspace/blackboard.sqlite`)
- One DB file globally; `task_id` column on every row.
- **Rejected for v0.5a** — corruption blast radius is the entire system. Cross-task analytics is not a v0.5a requirement (open question §8.1 of overview). Per-task DB also makes `--reset` trivial: copy file, swap task_id.

### C. PostgreSQL
- **Rejected** — operational overhead unjustified for single-machine MVP. No concurrent multi-task writes in v0.5a. Re-evaluate at v0.6 if a multi-tenant orchestrator daemon emerges.

### D. Embedded key-value (LMDB / RocksDB)
- **Rejected** — query patterns include `accepted_checks` lookup by `(task_id, check_key)` and `evidence` filtering by type. SQL is the right shape; schemaless KV would require building an index layer.

## Consequences

**Positive**
- Atomic `BEGIN`/`COMMIT` covers the full commit transaction (BestState + accepted_checks + candidate marker).
- `mypy --strict` clean: SQLiteRepository depends only on `sqlite3` from stdlib (zero new dependencies).
- File-based DB makes `--reset` simple: new task_id = new directory = new DB file (see ADR-005).
- WAL mode tolerates crash mid-transaction without corruption.
- Test ergonomics: `SQLiteRepository(":memory:")` for unit tests; `InMemoryRepository` retained for tests that don't need SQL semantics.

**Negative**
- No concurrent writers to the same `blackboard.sqlite`. Mitigation: CLI takes an advisory file lock on `blackboard.sqlite` before invoking Orchestrator (open question §8.1).
- Schema migrations are manual via `user_version` bump. Acceptable for v0.5a (one schema version).
- Cross-task analytics requires opening many DB files. Defer until needed.
- JSON payload columns lose query-ability (cannot index JSON fields without SQLite ≥3.45 generated columns). Acceptable: filter columns are denormalized (`status`, `gap_score`, `priority`, `verdict`).

## Trade-offs

ACID + zero ops > horizontal write scalability + cross-task query. v0.5a is a single-process, single-task-at-a-time orchestrator; SQLite matches that shape exactly. We pay no early complexity tax and can defer Postgres until requirements actually demand it.

## Compliance

- All `RepositoryProtocol` methods landing in v0.5a (PRD §16.3) must have a matching SQLite implementation with explicit transaction boundary.
- `commit_manager.apply` MUST execute its repo writes inside `with repo.transaction():` (new context-manager method on the protocol).
- `path_safety.resolve_workspace_path` rejects any path that would escape `workspace/tasks/<task_id>/`; the SQLite file lives inside this boundary, so workers cannot reach it through legitimate tool calls.
- `SQLiteRepository.__init__` MUST acquire an advisory file lock (`fcntl.flock(fd, LOCK_EX | LOCK_NB)`) on the `blackboard.sqlite` file. On `BlockingIOError`, raise `TaskAlreadyRunning(task_id, holder_pid)` so the CLI can print an actionable error. The lock is released on `SQLiteRepository.close()` or process exit. (Resolves overview §8 question 1.)
- `agent_specs` table is created by the schema for forward-compatibility but is NOT written by v0.5a. `repo.get_agent_spec(agent_id)` resolves from the in-process `AgentSpecRegistry`. (Resolves overview §8 question 3.)
