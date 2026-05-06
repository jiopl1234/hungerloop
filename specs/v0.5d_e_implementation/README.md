# v0.5d/e Implementation TODOs

Per-task implementation index for the v0.5b.2 → v0.5e.1 sequence.
Each row links to one of the five spec files that own its acceptance
criteria; the in-line task IDs (B2-01, D0-01, D1-01, E0-01, E1-01) are
the PR-sized chunks defined in each spec's §6 implementation TODO.

## Ordering

The sequence is **strictly linear**. Earlier releases gate later ones —
not for stylistic reasons but because each release's protocol methods
and SQL columns are load-bearing for the next:

1. **`v0.5b.2` (hard gate).** SQLiteRepository must ship before *any*
   v0.5d/e acceptance criterion is measurable. The PRD makes this
   explicit at §3.1.
2. **`v0.5d.0`.** Adds the protocol methods and event vocabulary that
   v0.5d.1 / v0.5e.0 / v0.5e.1 all consume. Don't try to land any of
   them in parallel.
3. **`v0.5d.1`.** ERROR-recovery gate, repair-state extensions,
   trace export `--include-global`. Last v0.5d release.
4. **`v0.5e.0`.** Memory predicates + lifecycle CLI + migration v5
   (memory section). Owns the `LATEST_VERSION = 5` bump.
5. **`v0.5e.1`.** SkillCardCandidate + deterministic derivation +
   skill CLI + migration v5 (skill section appended; see E1-01
   migration-strategy decision branch).

Within a release, individual task IDs (B2-01, B2-02, …) follow the
phase ordering documented in each spec's §6.

## File map

| Spec file | Release | LOC est. | Tasks | Depends on |
|---|---|---|---|---|
| [`v0.5b.2_sqlite_repository.spec.md`](./v0.5b.2_sqlite_repository.spec.md) | v0.5b.2 | ~1500 + tests | B2-01 → B2-17 | v0.5b/c shipped |
| [`v0.5d.0_observability_lifecycle_events.spec.md`](./v0.5d.0_observability_lifecycle_events.spec.md) | v0.5d.0 | ~600 + tests | D0-01 → D0-14 | v0.5b.2 |
| [`v0.5d.1_recovery_hardening.spec.md`](./v0.5d.1_recovery_hardening.spec.md) | v0.5d.1 | ~400 + tests | D1-01 → D1-14 | v0.5d.0 |
| [`v0.5e.0_memory_lifecycle.spec.md`](./v0.5e.0_memory_lifecycle.spec.md) | v0.5e.0 | ~500 + tests | E0-01 → E0-14 | v0.5d.1 |
| [`v0.5e.1_skill_card_lifecycle.spec.md`](./v0.5e.1_skill_card_lifecycle.spec.md) | v0.5e.1 | ~600 + tests | E1-01 → E1-14 | v0.5e.0 |

## Hard gates (cannot be skipped)

These are non-negotiable per the PRD. Skipping any of them breaks
downstream acceptance criteria:

| Gate | Where | Why |
|---|---|---|
| **SQLiteRepository ships before v0.5d.0** | `v0.5b.2 §1` | All "persists across restart" claims fail without it. |
| **`LATEST_VERSION = 4` bump only after both v3 and v4 commit** | `v0.5d.0 FR-19` | A half-applied migration leaves the events table without indexes; perf budgets fail. |
| **`expires_at` NOT re-added in v5** | `v0.5e.0 FR-19` | c0-01's v2 already added it; SQLite will throw `duplicate column name` on second run. |
| **`SkillCardCandidate` is deterministic** | `v0.5e.1 NFR-1, NFR-2` | The §18.4 helpers must be pure; no LLM in the path. Pin via golden test. |
| **Migration strategy decision (E1-01) before any v0.5e.1 work** | `v0.5e.1 FR-15` | Path A vs Path B determines whether the migrator itself needs to grow addendum support. |

## Decisions in force

Carried forward from v0.5b/c (still binding for v0.5d/e):

1. `report` and `status` markdown are **distinct templates** — no
   shared formatter.
2. Migration backups: keep latest + 2 prior; prune older to
   `<workspace>/.archive/`.
3. Stale-lock threshold: env `HUNGERLOOP_LOCK_STALE_SEC` (default 30
   min) + CLI `--lock-stale-sec N`.
4. MemoryCandidate expiry: time-based, 90-day default; auto-job
   deferred to v0.6.
5. DummyModelClient: warning-only on YAML loader path; test-injection
   silent; `HUNGERLOOP_QUIET_DUMMY=1` suppresses.

New for v0.5d/e (locked 2026-05-06):

6. **`event_type.value` is the wire contract.** Renaming any shipped
   `EventType` member is forbidden; only additive expansion. PRD
   §7.2 enumerates protected names.
7. **`MemoryCandidate.candidate_id` and `MemoryCandidate.state` are
   shipped fields.** Do not rename to `id` / `status`. PRD §14.1.
8. **`MemoryType.procedure` shipped value preserved.** v0.5e.0 expands
   the literal additively; `procedure` is NOT renamed to
   `procedural_rule`.
9. **`is_reusable` uses anchored regex, not substring match.** Corpus
   test pins false-positive rate at zero. PRD §15.3.
10. **`trace export` excludes global events by default.**
    `--include-global` opts in; the SQL `SELECT WHERE task_id = ?`
    semantic is the contract. PRD §12.3.
11. **ERROR resume requires a `repair_state_action` event newer than
    the StopReport.** `--skip-repair-check` is the operator override
    and writes its own audit row. PRD §13.1.
12. **SkillCard derivation is deterministic and LLM-free.** PRD §18.4
    pseudocode + §18.5 golden test pin reproducibility.

## Cross-cutting test infrastructure

These test utilities ship once and get reused across releases:

| Utility | Lands in | Used by |
|---|---|---|
| `test_repository_protocol_parity.py` framework | B2-11 | every later release adds parametrized assertions |
| Subprocess-based restart parity harness | B2-12 | E0-13 (memory restart), E1-13 (skill restart) |
| Synthetic-worker-exception fixture | D0-13 | D1-13 (full chain), E0/E1 integration |
| `repair_state_action` event audit pattern | D1-* | E0-* approve/reject events follow the same shape |
| Determinism golden-test pattern | E1-05 | future v0.6 derivation iterations reuse |

When a later release needs an existing utility, reuse it — do not
re-fork. Adding parametrized cases to the parity framework is a
one-line change in most cases.

## Migration version timeline

| Version | Owner | Files | What | Status |
|---|---|---|---|---|
| v1 | shipped | `v1__initial.sql` | Full schema baseline | shipped (b0-02) |
| v2 | shipped | `v2__memory_candidate_lifecycle.sql` | MemoryCandidate lifecycle columns + `idx_memory_state` | shipped (c0-01) |
| v3 | v0.5d.0 | `v3__usage_snapshots.sql` | `usage_snapshots` table | pending |
| v4 | v0.5d.0 | `v4__observability_indexes.sql` | Indexes for events / loop_traces / stop_reports / accepted_checks / hunger_items / evidence / worker_results | pending |
| v5 | v0.5e.0 | `v5__memory_skill_lifecycle_extensions.sql` | Memory predicates / provenance / review columns + `promoted_memories` table | pending |
| v5 (cont.) | v0.5e.1 | append OR `v5_addendum_skill_tables.sql` | `skill_card_candidates` + `active_skill_cards` tables | pending; depends on E1-01 decision |

`SQLiteMigrator.LATEST_VERSION` ends up at `5` after v0.5e.1 ships.

## Acceptance test count target

Each release targets at minimum:

| Release | New tests | Cumulative |
|---|---|---|
| v0.5b.2 | ~50 (B2-11 parity, B2-12 restart, B2-13 lock, B2-14 perf, per-section unit tests) | ~540 |
| v0.5d.0 | ~30 (event ordering, schema additions, migration v3/v4) | ~570 |
| v0.5d.1 | ~25 (preflight matrix × 7 stop reasons, D8-D13 detectors, full-chain integration) | ~595 |
| v0.5e.0 | ~30 (predicate computation, corpus test, approve/reject branches, migration v5) | ~625 |
| v0.5e.1 | ~25 (trigger rule × 5 conditions, determinism golden, export/import round-trip) | ~650 |

Baseline at v0.5b/c ship: 488 + 2 perf = 490. Target at v0.5e.1
ship: ~650 unit + integration tests, all green under
`pytest tests/ -q`.

## How to consume this folder

For each task chunk (e.g. B2-03, D0-04, E1-08):

1. Open the owning spec file and find the §6 task entry.
2. Read the linked PRD sections referenced from the spec.
3. Run the listed tests locally first to confirm the baseline.
4. Implement the checklist top-to-bottom (the order is dependency-
   ordered within each task).
5. The spec's §9 "Definition of done" is the merge gate.

When in doubt about scope: the §8 "Out of scope" or "Non-goals"
sections in each spec are authoritative — defer additions to a later
release rather than expanding the current one.

## Quick reference: where to look first

| Question | Answer |
|---|---|
| "Where do I start?" | `v0.5b.2_sqlite_repository.spec.md` §6 → B2-01 |
| "Why does this exist?" | `hungerloop_v0_5d_e_prd_revised.md` §0 + §3.1 |
| "What's already shipped?" | PRD §2 status table |
| "What's still forbidden?" | PRD §26 developer warning list |
| "What's the wire contract for events?" | PRD §7.2 + §7.5 |
| "How do I add a new EventType?" | Append to `models/events.py` enum; never rename |
| "How do I add a new protocol method?" | Add to `RepositoryProtocol` + both backends + parity test |
| "Why are there two `*_check_keys` fields on MemoryCandidate?" | PRD §14.4 |
| "Why isn't `expires_at` in v5?" | PRD §20.2 (already in v2) |
| "How do I share a skill across machines?" | `skill export <id> --output skill.yaml` then `skill import skill.yaml` (E1-11/E1-12) |
