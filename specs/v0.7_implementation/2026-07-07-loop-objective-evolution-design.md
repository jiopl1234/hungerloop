# v0.7 Loop-Objective Evolution — Design

Date: 2026-07-07
Status: Delivered (all phases implemented and validated)
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

## 8. Delivered scope

All three phases have been implemented, tested, and validated:

- **Phase 1 (SpecCheckSynthesizer)**: `CheckProposal` model,
  `CheckProposalGate` with deterministic dry-run validation,
  `RefinementCompiler.compile_spec_coverage` compiler-owned injection,
  `SpecCheckSynthesizer` with cost-guarded LLM calls, plan-time and
  post-commit synthesis wiring behind `synthesis_enabled` (default off).
  Real `glm-5.2` smoke validated safely with secret-scan evidence.

- **Phase 2 (Refactor transactions)**: `RefactorTransaction` model,
  repository persistence with SQLite v7 migration,
  `select_commit_candidate` deterministic score-free selection,
  `RefactorTransactionManager` with open/settle/rollback lifecycle,
  `CommitManager` transaction-aware I-3 amendment (ADR-010),
  stagnation exemptions for declared regression keys, orchestrator
  wiring. Default off via `refactor_transactions_enabled=False`.

- **Phase 3a (Discovery credit)**: `HandoffItem.proposed_checks`,
  `HandoffProcessor` proposal routing with `CheckProposalGate`,
  `DISCOVERY_CREDIT` events, stop-report summaries, no-progress-streak
  reset at most once per loop, per-loop cap enforcement.

- **Phase 3b (Memory promote and recall)**: `MemoryManager.auto_promote`
  with predicate gating, upgraded memory candidate content with
  prompt-safe evidence digests, `ContextPack.recalled_memories`,
  `ContextBuilder` cross-task recall (top 5, 1200 chars),
  `ExecutionWorker` prior-mission insights rendering.

### Policy defaults (v0.6 compatibility)

| Flag | Default | Effect |
|------|---------|--------|
| `synthesis_enabled` | `False` | No synthesis calls, no credential reads |
| `synthesis_plan_time_tier` | `0` | Co-equal with operator checks when enabled |
| `synthesis_max_total_items` | `20` | Lifetime cap on synthesized items |
| `refactor_transactions_enabled` | `False` | Strict I-3 preserved, stale rows ignored |
| `max_declared_regressions` | `5` | Maximum declared regression keys per transaction |
| `refactor_deadline_loops` | `3` | Deadline window in loops |
| `memory_auto_promote_enabled` | `True` | Additive auto-promotion after DONE |
| `memory_recall_enabled` | `True` | Additive cross-task recall into context |

### Approved baseline deselection set

The final pytest gate uses exactly this approved baseline deselection
set and no additional skips, xfails, ignores, or `-k` workarounds:

- `tests/unit/test_loop_orchestrator.py::test_orchestrator_uses_validation_pipeline_and_commit_receives_result`
- `tests/integration/test_loop_memory_dummy.py::test_loop_memory_dummy_propagates_failure_to_next_prompt`
- `tests/integration/test_mission_resume.py::test_mission_run_resume_after_sigterm_mid_validating`
- `tests/unit/test_cli_workspace_checks.py::test_workspace_best_lists_files_with_sizes`
- `tests/unit/test_mission_cmd_edit.py::test_edit_allows_when_task_record_status_human_paused`
- `tests/unit/test_mission_cmd_edit.py::test_edit_invokes_import_and_records_applied_event`
- `tests/unit/test_mission_cmd_edit.py::test_edit_editor_nonzero_cancels_without_mission_writes`
- `tests/unit/test_mission_cmd_edit.py::test_edit_editor_nonzero_preserves_sqlite_mission_tables`
- `tests/unit/test_mission_cmd_edit.py::test_edit_empty_buffer_cancels_without_mission_writes`
- `tests/unit/test_path_safety.py::test_absolute_path_rejected`
- `tests/unit/test_path_safety.py::test_symlink_escape_rejected`
- `tests/unit/test_sandbox_runner.py::test_argv_execution_success`
- `tests/unit/test_sandbox_runner.py::test_argv_execution_failure`
- `tests/unit/test_sandbox_runner.py::test_timeout_returns_timed_out`
- `tests/unit/test_sandbox_runner.py::test_timeout_kills_process_group`
- `tests/unit/test_sandbox_runner.py::test_evidence_saved`
- `tests/unit/test_sandbox_runner.py::test_stderr_captured`
- `tests/unit/test_sandbox_runner.py::test_nonzero_exit_keeps_stdout`
- `tests/unit/test_tool_harness.py::test_run_shell_attaches_sandbox_evidence`
- `tests/unit/test_tools.py::test_run_shell_argv_only`

### Final validation gates

- **pytest**: `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`
  with exactly one `--deselect` per approved baseline test above.
- **mypy**: `.venv\Scripts\python.exe -m mypy --strict --no-incremental src`
- **ruff**: `.venv\Scripts\python.exe -m ruff check src tests --no-cache`
- **CLI smoke**: `hungerloop --version` and `hungerloop mission --help`
- **Real LLM smoke**: model `glm-5.2`, minimal request, secret-scan evidence.
- **No ports or persistent services** remain after validation.

### Placeholder spec references

- `specs/v0.7_placeholders/llm_planner.md` - partially delivered
  (synthesis component only; full LLM mission planner remains future work).
- `specs/v0.7_placeholders/concurrent_fan_out_and_join.md` -
  commit-selection interface delivered; concurrent scheduling remains
  future work.
- `specs/v0.7_placeholders/cross_task_memory_recall.md` - delivered
  and linked to this spec.
- `specs/v0.7_placeholders/services_yaml_rich_semantics.md` - not
  delivered (future work).
- `specs/v0.7_placeholders/web_ui.md` - not delivered (v0.8+ future work).

## 9. Post-GA hardening delta (2026-07-12 → 2026-07-16)

Bench-driven hardening commits (`c93df26`, `8b6c894`, plus follow-up
fixes) extended the delivered scope. Strict I-3 remains the default; a
regression is never cleared without fresh passing evidence, and none of
the additions below use score in any decision.

### Worker repair convergence

- `read_file` accepts `offset`/`limit` (1-based, default 200 lines, max
  400) and labels results `[lines X-Y of N]`; successful reads persist a
  capped `output_excerpt` in evidence.
- `patch_file` accepts optional `start_line`/`end_line` anchors and a
  whitespace-normalized fallback match. The fallback requires an anchor
  or a multi-line `old_text`, preserves per-line source indentation, and
  rejects (rather than guesses) when the replacement changes line count
  or intended indentation.
- Read-only stalls are bounded mechanically: after two consecutive
  non-writing action batches (empty batches count) with no successful
  write in the loop, further non-writing actions are refused with
  `read_only_budget_exhausted`. `WORKER_READ_ONLY_STREAK` events record
  per-agent streaks across loops.
- Rejected-candidate continuation: an uncommitted prior candidate tree
  may seed the next loop's candidate (never `best/`, preserving I-4).
  Gated by `rejected_candidate_continuation_enabled` (default `True`)
  and `rejected_candidate_continuation_max_chain` (default `2`);
  abandoned when the same regression key repeats across consecutive
  rejected loops. Events: `CANDIDATE_CONTINUATION_SEEDED` /
  `CANDIDATE_CONTINUATION_SKIPPED`.
- Regression confirmation: `regression_confirm_reruns` (default `2`)
  re-runs each regressed check; the regression is cleared only when
  every rerun passes (the first failing rerun stops the spend). The
  clearing event is `CHECK_REGRESSION_DISCONFIRMED` (renamed from the
  misleading `check_regression_reconfirmed`).

### Synthesized-check validity

- The proposal gate statically `compile()`s `python -c` snippets
  (`argv[1] == "-c"` only) and classifies dry-run failures
  (`syntax_error`/`setup_error`/`timeout`) from the terminal stderr line
  of non-zero exits, rejecting `assertion_not_executable` proposals.
- Baseline validation only reconciles regressions after verifying the
  validated workspace matches the recorded best manifest
  (`SYNTH_BASELINE_IDENTITY_MISMATCH` otherwise); the rewritten
  effective report is persisted with a distinct id alongside the raw
  report. Every writer of `best/files` (promote, mission-state
  regeneration, refactor rollback) rebuilds `best/manifest.json` so the
  identity check stays truthful.
- Post-commit synthesis backfill stops early on zero actionable yield
  (`SYNTHESIS_BACKFILL_STOPPED`), and the synthesis model is selectable
  via `HUNGERLOOP_SYNTHESIS_MODEL` (default `glm-5.2`).

### Policy additions

| Flag | Default | Effect |
|------|---------|--------|
| `regression_confirm_reruns` | `2` | Reruns required to clear a regressed check; every rerun must pass |
| `rejected_candidate_continuation_enabled` | `True` | Seed next candidate from the prior rejected tree |
| `rejected_candidate_continuation_max_chain` | `2` | Consecutive continuation limit before reset to best |
