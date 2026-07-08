# ADR-010: Refactor Transactions with Bounded Non-Monotonic Commit Windows

## Status

Status: Accepted (2026-07-08)

## Context

Invariant I-3 (check-level commits) requires that promotion to `best/` demands
`newly_passed_check_keys` is non-empty, no regressions exist, and evidence is
present. This strict monotonic-progress guarantee is correct for normal
hunger-driven loops: each committed candidate must strictly advance the
accepted-check set without breaking anything that previously passed.

However, v0.7 introduces **refactor transactions** (see the v0.7 architecture
spec). A refactor is a deliberate, bounded restructuring where a worker
intentionally breaks or removes previously-passing checks (e.g., deleting old
test files, renaming APIs, splitting modules) and then rebuilds them with
net-new acceptance checks before settlement. Under strict I-3, every
intermediate loop during such a refactor would be rejected because the
regressions would block promotion, even though the worker has a plan to recover
them.

Without a bounded exception, refactors are impossible: the worker can never
commit intermediate progress, the no-progress streak grows, and the loop stalls
or stops. The only workaround would be to disable I-3 globally, which would
undermine the invariant for all tasks and all loops.

### Constraints

- I-3 must remain strict by default. Commits must never use score as a
  decision factor, no global regression tolerance, no weakening of evidence
  requirements. This is the only approved I-3 amendment.
- The exception must be opt-in via policy (`refactor_transactions_enabled`).
- The exception must be bounded: only declared regression keys are tolerated,
  only while a matching transaction is open, and only until a deadline.
- Settlement must enforce net progress: all declared keys must recover and the
  accepted-check set must be a strict superset of the baseline.
- Failure to settle must roll back to the exact baseline best state and files.
- The exception must not allow workers to supply or extend deadlines.
- Score must never participate in commit selection or gate decisions.

## Decision

We introduce **ADR-010**: a bounded, policy-gated, deadline-limited exception
to I-3 that applies only while a matching open refactor transaction is active.

### Rules

1. **Enabled-policy gate.** Refactor transaction tolerance is disabled by
   default (`refactor_transactions_enabled=False`). When disabled, no
   transaction behavior occurs, stale transaction rows are ignored, and strict
   I-3 is preserved exactly as in v0.6.

2. **Declared-regression tolerance.** While a transaction with `status="open"`
   and matching `task_id` is active, `CommitManager.apply` tolerates
   regressions only for check keys listed in
   `RefactorTransaction.declared_regression_keys`. Any regressed check key
   not in the declared set still rejects the candidate. Closed, rolled-back,
   wrong-task, or stale transactions provide no tolerance.

3. **Deadline-bounded.** A transaction's `deadline_loop` is derived as
   `opening_loop + policy.refactor_deadline_loops`. Worker handoff payloads
   cannot supply, extend, or override this deadline. When the deadline is
   reached, the transaction must settle (succeed or roll back).

4. **Rollback-on-failure.** Settlement succeeds only when:
   - Every declared regression key is passing again.
   - The current accepted-check key set is a strict superset of the baseline
     accepted-check key set (not merely a larger count).

   If settlement fails, the manager restores `best/` files and `BestState`
   from the transaction snapshot, marks the transaction `rolled_back`, and
   emits a `REFACTOR_TXN_ROLLED_BACK` event. The system returns to the exact
   pre-transaction state.

5. **Score-free commit selection.** `select_commit_candidate(evals)` is a pure
   deterministic function that orders candidates by:
   - Most unique newly-passed check keys (descending).
   - Fewest unique failing check keys (ascending).
   - Lexicographic candidate id (ascending, stable tie-breaker).

   No `BestState.score`, `CandidateState.proposed_score`, or any score-derived
   field participates in selection or gate decisions. `BestState.score` remains
   schema-only at 0.0.

6. **Single-open enforcement.** At most one open transaction may exist per
   task. Opening a second transaction is rejected. Closed or rolled-back
   transactions do not count as open.

7. **Non-monotonic only for declared keys.** The tolerance is surgical:
   only the explicitly declared regression check keys may temporarily fail.
   All other I-3 conditions (non-empty `newly_passed_check_keys`, evidence
   present, verdict PASS or PARTIAL) remain mandatory even inside a
   transaction.

## Consequences

### Positive

- Refactors can proceed with intermediate commits that temporarily break
  declared checks, enabling incremental restructuring without stalling the loop.
- The exception is bounded and auditable: every open, close, and rollback
  emits stable non-secret events.
- Strict I-3 is preserved for all non-transaction scenarios and for all
  undeclared regressions.
- Score remains excluded from all commit decisions, preserving the v0.6
  invariant.

### Negative

- The commit gate is more complex: `_can_commit` now takes an optional
  transaction argument and must check declared regression keys.
- Stagnation detection must be aware of declared regression keys to avoid
  penalizing the loop for regressions that are intentionally tolerated.
- Rollback requires exact snapshot restoration, adding filesystem and state
  management complexity.
- The exception must be carefully tested to ensure closed, rolled-back,
  wrong-task, and stale transactions never relax I-3.

### Neutral

- `select_commit_candidate` is a new pure function prepared for future
  fan-out scenarios. It is not yet wired into the single-candidate
  production path but is available for multi-candidate selection when
  concurrent fan-out is implemented.
