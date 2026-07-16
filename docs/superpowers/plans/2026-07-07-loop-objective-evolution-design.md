# v0.7 Loop-Objective Evolution — Design

Date: 2026-07-07
Status: Approved design (brainstorm complete; implementation plan to follow)
Replaces placeholders: `specs/v0.7_placeholders/llm_planner.md` (partially),
`specs/v0.7_placeholders/concurrent_fan_out_and_join.md` (commit-selection
interface only), `specs/v0.7_placeholders/cross_task_memory_recall.md`.

## 1. Motivation

Benchmark evidence (llm-bench regex-01, filter-expr-01, mini-regex/mini-sql
rounds documented in the hungerloop-tuning operator notes) shows three
structural gaps in the v0.6 loop:

1. **Frozen objective.** The hunger ledger is compiled once at plan time.
   Spec behaviors not compiled into checks are invisible to the loop:
   regex-01 shipped a nested-group numbering bug after 5 loops / 396K
   tokens with a green 25/25 ledger, because no check exercised nesting
   while the spec prose contained a machine-extractable example.
2. **No bounded non-monotonicity.** The I-3 no-regression gate forbids
   "go down to go up" restructuring; search is single-lineage greedy
   hill-climbing that cannot take strategically regressive paths.
3. **Misaligned discovery incentives and dormant memory.** A worker that
   discovers an unchecked spec violation gains nothing by reporting it.
   Layer-3 memory (candidates/skill cards) is written but never promoted
   automatically and never recalled into any context.

This design adds three phases, each independently shippable.

## 2. Non-goals and red lines

- No concurrent scheduling in this milestone (only the deterministic
  commit-selection interface that future fan-out will feed).
- No Web UI work.
- Unchanged CI contract: no LLM and no `ModelClient` imports under
  `services/validators/`; ledger writes only via `requirement_compiler.py`
  / `refinement_compiler.py`; `mission_state_updater.py` stays read-only
  toward the repository.
- The only invariant amended is **I-3** (refactor-transaction exception,
  Section 4). The amendment ships with an ADR and a CLAUDE.md update.
- All new behavior is flag-gated. Defaults: synthesis **off**, refactor
  transactions **off**, memory auto-promote/recall **on** (each with its
  own kill switch). Existing missions behave identically with defaults
  unless flags are enabled.

## 3. Phase 1 — SpecCheckSynthesizer (acceptance-set co-evolution)

### 3.1 Component

New service `src/hungerloop/services/spec_check_synthesizer.py`.
Uses `ModelClient` (plan-time / post-commit only; never inside
validators). Every LLM call is wrapped by
`CostGuard.assert_within_budget()` before and after (I-8).

Input: `mission.md` prose + `features[].description` (the full task
brief), plus, in incremental mode, a digest of already-covered check
descriptions. Output: a list of `CheckProposal` items (new Pydantic
model in `models/synthesis.py`, shared with Phase 3a worker proposals):

- `check_type`: restricted to `AcceptanceCheckType.SHELL_EXIT_ZERO` or
  `AcceptanceCheckType.FILE_EXISTS` (nothing else is accepted, in
  particular not `LLM_JUDGE`).
- `params`: argv / path as required by the check type.
- `source_quote`: verbatim spec excerpt that motivates the check
  (audit anchor; required, non-empty).
- `dedup_key`: normalized-argv hash; proposals matching an existing
  ledger check or a previously rejected/accepted proposal are dropped.

### 3.2 Injection points

- **Plan time** (`mission import` / `mission new`, before loop 1):
  accepted proposals are passed into `RequirementCompiler` and land as
  regular acceptance checks at tier `synthesis_plan_time_tier`
  (default 0, i.e. co-equal with operator checks).
- **Post-commit** (incremental): after each successful commit,
  `loop_orchestrator` invokes the synthesizer in incremental mode.
  Accepted proposals route through `RefinementCompiler` and land as
  `HungerItem`s with `refinement_tier=1`,
  `refinement_kind="spec_coverage"`,
  `generated_by="spec_check_synthesizer"`, and `source_check_keys`
  linking back to the motivating feature. Existing tier gating
  (`RefinementCompiler.ensure_next_tier`) activates them only after
  tier 0 completes, so `HungerLedger.is_done()` no longer equals
  "original checklist green".

Caps: per-loop injections bounded by `budget.max_new_items_per_loop`;
lifetime bounded by new policy field `synthesis_max_total_items`.

### 3.3 Proposal validation gate (deterministic, pre-injection)

Shared component (reused by Phase 3a): `services/check_proposal_gate.py`.

1. argv allowlist (configurable; default `python`, `python -m pytest`).
2. `path_safety` validation of any path params (I-7).
3. Sandbox dry-run **twice** via `SandboxRunner`; the proposal must
   execute (any exit code) and produce identical pass/fail outcomes on
   both runs (rejects nondeterministic checks).
4. Rejections emit event `SYNTH_CHECK_REJECTED` with the reason and
   `source_quote`.

### 3.4 Config

New `HungerPolicy` fields (policy-driven, I-10):
`synthesis_enabled: bool = False`, `synthesis_plan_time_tier: int = 0`,
`synthesis_max_total_items: int = 20`.

## 4. Phase 2 — RefactorTransaction + commit-selection interface

### 4.1 Model and storage

`models/refactor.py::RefactorTransaction`:
`transaction_id`, `task_id`, `declared_regression_keys` (R),
`opened_at_loop`, `deadline_loops` (K, default 3),
`baseline_best_state_id`, `status: open | closed_success | rolled_back`.
New SQLite table `refactor_transactions` + migration. Repository
protocol gains save/get/list/update for transactions.

### 4.2 Lifecycle

- **Open**: worker emits a handoff item of new type `refactor_proposal`
  (declares R + rationale). `HandoffProcessor` routes it to new service
  `services/refactor_transaction_manager.py`, which approves by
  deterministic rules: `R ⊆ currently accepted check_keys`,
  `|R| <= max_declared_regressions` (policy, default 5), at most one
  open transaction per task. On open, snapshot `best/files` to
  `best/.txn_<transaction_id>/` (baseline restore source) and record
  `baseline_best_state_id`.
- **During (I-3 amendment)**: while a transaction is open,
  `CommitManager` accepts a candidate iff (a) zero regressions outside
  R, and (b) `newly_passed_check_keys != ∅`. Regressions inside R are
  tolerated and tracked on the transaction.
- **Settle** (automatically at `opened_at_loop + deadline_loops`, or
  earlier when the worker emits a `refactor_proposal` handoff item with
  `action="close"`): if every key in R passes again AND accepted checks
  are a strict superset of the baseline's, mark `closed_success`.
  Otherwise **roll back**: restore `best/files` from the snapshot,
  point best back to `baseline_best_state_id`, mark `rolled_back`,
  emit event `REFACTOR_TXN_ROLLED_BACK`.
- **Stagnation interplay**: while open, failures of checks in R do not
  increment `consecutive_failure_count` (I-6 attempted-only semantics
  unchanged; only the R exclusion is added).

### 4.3 Commit-selection interface (fan-out ready)

New pure function
`services/commit_selection.py::select_commit_candidate(evals) -> winner | None`
with deterministic ordering: gate-passing candidates first, then
`|newly_passed|` descending, then failing-check count ascending, then
`candidate_id` lexicographic. **No score is consulted** (I-3).
`CommitManager` is refactored to evaluate via a single-element list
today; future fan-out feeds multiple candidates with zero change to
commit semantics.

### 4.4 Documentation deliverables

ADR `docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md` and a
CLAUDE.md I-3 amendment ship in the same PR as the code.

Config: `refactor_transactions_enabled: bool = False`,
`max_declared_regressions: int = 5`, `refactor_deadline_loops: int = 3`
on `HungerPolicy`.

## 5. Phase 3 — Discovery credit + layer-3 memory

### 5.1 Phase 3a: discovery credit

- `HandoffItem` gains optional `proposed_checks: list[CheckProposal]`
  (used only with `item_type="discovered_issue"` / kind `test_gap`).
- `HandoffProcessor` passes proposals through the **same**
  `check_proposal_gate` as Phase 1, then injects via
  `RefinementCompiler` (tier 1, `generated_by=<agent_id>`).
- New event `DISCOVERY_CREDIT`: emitted the first time a
  worker-generated check appears in `newly_passed_check_keys`
  (payload: proposer agent_id, check_key, loop_id). `StopReport` and
  the auto skill card summarize credits per agent.
- Anti-gaming: an accepted proposal counts toward
  `has_real_progress` at most once per loop (bounds no-progress-streak
  resets); injection volume stays under the existing
  `max_new_items_per_loop` cap.

### 5.2 Phase 3b: memory promote + recall

- `MemoryManager.auto_promote(task_id)`: runs after a DONE
  `StopReport`; promotes every candidate whose four existing predicates
  (`action_verified`, `reusable`, `non_volatile`, `traceable`) are all
  true via `repo.save_promoted_memory` (state `promoted`, event
  emitted). The `HUMAN_APPROVAL` H-003 path remains available.
- **Candidate content upgrade**: replace the low-signal
  `"Verified acceptance check <key>"` string with "check description +
  key tool-sequence digest" extracted from the loop's evidence
  (<= 300 chars), so promoted rows are reusable insight rather than
  bookkeeping noise.
- **Recall**: `ContextBuilder.build_for_agent` reads promoted memories
  **across all tasks** (top-5 by `created_at` descending; contents are
  guaranteed task-agnostic by the `reusable` predicate) into a new `ContextPack`
  field `recalled_memories` (<= 1200 chars, participating in the
  existing history-cap accounting).
- Config: `memory_auto_promote_enabled: bool = True`,
  `memory_recall_enabled: bool = True` on `HungerPolicy`.

## 6. Cross-cutting

- **Quality gates**: `mypy --strict src/`, `ruff check src/ tests/`,
  full suite green (baseline ~1099 passing; the one pre-existing
  `test_loop_orchestrator.py` spy-signature failure is out of scope).
- **Testing**: unit tests per new service/model; at least one mission
  integration test per phase, including the transaction rollback path,
  all against `SQLiteRepository` (InMemory previously masked the
  handoff-id collision bug).
- **Migrations**: one new table (`refactor_transactions`);
  `promoted_memories` already exists; events reuse the existing table.
- **Delivery order**: Phase 1 → Phase 3a (shares the proposal gate) →
  Phase 2 (independent) → Phase 3b (most independent).
- **Risks and mitigations**:
  - Synthesized-check quality → dry-run-twice determinism gate +
    `source_quote` auditability + volume caps.
  - Rollback correctness → snapshot-restore integration test, SQLite.
  - Recall polluting context → char cap + `reusable` predicate.
  - I-3 amendment drift → ADR-010 + CLAUDE.md update in the same PR.

## 7. Resolved decisions

1. Synthesis timing: plan-time + post-commit incremental (option B).
2. Non-monotonicity scope: transactions + selection interface, no
   concurrent scheduling yet (option B).
3. Layer-3 memory: auto-promote + recall wired (option A).
4. Delivery: one combined design, three independently shippable phases.
