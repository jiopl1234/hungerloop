# v0.5b/c Implementation TODOs

Per-task implementation files split out of `specs/v0.5b_c_prd_enhancements.spec.md`. Each file is one PR's worth of work: scope, files to touch, checklist, test plan, acceptance link.

## Ordering

1. **Do `00-prd-edits.md` first.** Until the 8 PRD insertions land, every other file is implementing against a draft. The PRD edits are mostly mechanical paste-ins from the spec — half a day of work, unblocks everything else.
2. **v0.5b.0 group (`b0-*`)** is the release-blocker set. All five must land before the `v0.5b.0` tag.
3. **v0.5b.1, v0.5c.0, v0.5c.1 groups** can land in any order within their release.

## File map

| File | Section in spec | Release | LOC est. |
|---|---|---|---|
| [`00-prd-edits.md`](./00-prd-edits.md) | (all) | pre-work | doc-only |
| [`b0-01-report-cmd.md`](./b0-01-report-cmd.md) | §1 | v0.5b.0 | ~150 |
| [`b0-02-migrator.md`](./b0-02-migrator.md) | §2 | v0.5b.0 | ~250 |
| [`b0-03-repair-state.md`](./b0-03-repair-state.md) | §3 | v0.5b.0 | ~250 |
| [`b0-04-task-lock.md`](./b0-04-task-lock.md) | §4 | v0.5b.0 | ~120 |
| [`b0-05-event-vocab.md`](./b0-05-event-vocab.md) | §7 (enum part) | v0.5b.0 | ~80 + mech. callers |
| [`b1-01-cost-recon.md`](./b1-01-cost-recon.md) | §6 | v0.5b.1 | ~50 |
| [`b1-02-dummy-config.md`](./b1-02-dummy-config.md) | §8 | v0.5b.1 | ~60 |
| [`c0-01-memory-fields.md`](./c0-01-memory-fields.md) | §5 | v0.5c.0 | ~40 + schema |
| [`c1-01-trace-export.md`](./c1-01-trace-export.md) | §7 (export) + §9 perf | v0.5c.1 | ~100 |

## Decisions in force (from spec §11, locked 2026-05-04)

1. `report` and `status` markdown are **distinct templates** — no shared formatter.
2. Migration backups: keep latest + 2 prior; prune older to `<workspace>/.archive/`.
3. Stale-lock threshold: env `HUNGERLOOP_LOCK_STALE_SEC` (default 30 min) + CLI `--lock-stale-sec N`.
4. MemoryCandidate expiry: time-based, 90-day default; auto-job deferred to v0.6.
5. DummyModelClient: warning-only on YAML loader path; test-injection silent; `HUNGERLOOP_QUIET_DUMMY=1` suppresses.

## How to consume this folder

For each task file:

1. Read the linked spec section for context (EARS reqs, acceptance criteria).
2. Run the listed tests locally first to confirm the baseline.
3. Implement the checklist top-to-bottom (the order is dependency-ordered).
4. The "Done when" criteria are the merge gate.
