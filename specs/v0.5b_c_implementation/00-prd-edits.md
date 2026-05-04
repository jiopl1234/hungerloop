# 00 — PRD edits (do first)

**Goal**: Land the 8 PRD insertions/expansions from `specs/v0.5b_c_prd_enhancements.spec.md` into `hungerloop_v0_5b_c_prd.md` so subsequent implementation work has a stable spec to compile against.

**Type**: documentation only, no code.

## Checklist

- [ ] **§18.3** `hungerloop report` — paste from spec §1 (recommended design + EARS + JSON schema example). Cross-link from §1.1 v0.5b.0 scope.
- [ ] **§5.5** SQLite migrations — paste from spec §2. Reference the `LATEST=1` pin and the `migrations/v{N}__{slug}.sql` convention. Mention backup retention rule (latest + 2 prior, archive older).
- [ ] **§16.3 expansion** — replace the current 3-line repair-state placeholder with spec §3's D1-D7 divergence table + `--check`/`--fix` policy + exit codes.
- [ ] **§5.1.1** Task lock fault recovery — paste spec §4. Document env var `HUNGERLOOP_LOCK_STALE_SEC` and `--lock-stale-sec` flag. Cross-link to `repair-state` D6.
- [ ] **§19.1 expansion** — add the four new MemoryCandidate fields (`state`, `decision_loop_id`, `decided_by`, `decision_rationale`, `replaces_candidate_id`) plus the v0.5c-emit-only rule. Mention 90-day expiry semantic for v0.6 (no implementation in v0.5c).
- [ ] **§8.7.1** Cost reconciliation — paste spec §6. Reference threshold env `HUNGERLOOP_COST_DELTA_THRESHOLD` (default 0.20) and the non-retroactive rule.
- [ ] **§22.8** Event vocabulary — paste spec §7. Include the full `EventType` enum and the additive-only rule.
- [ ] **§6.4** DummyModelClient long-term contract — paste spec §8. Document `provider: dummy` first-class, the action-schema freeze, the warning-only policy, and `HUNGERLOOP_QUIET_DUMMY=1`.

## Cross-cutting edits

- [ ] **§1.1 v0.5b.0 scope** — extend the bullet list with: `hungerloop report`, schema migration framework, `repair-state --check`, stale-lock recovery, `EventType` enum.
- [ ] **§1.2 v0.5b.1 scope** — add: cost reconciliation event, `provider: dummy` first-class.
- [ ] **§1.3 v0.5c.0 scope** — add: MemoryCandidate forward-compat fields.
- [ ] **§1.4 v0.5c.1 scope** — add: `hungerloop trace export --format jsonl`, performance test.

## Done when

- [ ] PRD compiles cleanly (manual read-through; no broken section references).
- [ ] `grep -n "TBD\|TODO" hungerloop_v0_5b_c_prd.md` returns nothing new.
- [ ] All 8 spec sections have a corresponding PRD section number in the spec's blocking matrix (§ "Blocking-vs-deferrable matrix").
- [ ] Commit message: `docs(v0.5b-c): land enhancement spec sections (report/migrator/repair-state/lock/memory/cost-recon/events/dummy)`.

## Why this is the gate

Implementation files reference PRD section numbers. If PRD edits land second, every implementation PR needs a follow-up to update internal links once the PRD section numbers settle.
