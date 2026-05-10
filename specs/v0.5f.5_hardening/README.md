# v0.5f.5 Hardening — Deferred items from PR #1 review

**Status:** placeholder — items below were deferred from the
v0.5f code review (see PR #1) so the budgeted-refinement PR
could land without blocker-class fixes lingering.

**Parent specs:**
- [`v0.5f_loop_memory.spec.md`](../v0.5f_loop_memory.spec.md)
- [`v0.5f_implementation/v0.5f.4_budgeted_refinement_worker.spec.md`](./../v0.5f_implementation/v0.5f.4_budgeted_refinement_worker.spec.md)

## Why this exists

PR #1's reviewer flagged 8 Important + several Minor findings
against the v0.5f.4 budgeted refinement work. Three of them
(MAX_HISTORY_CHARS clip, `--refinement-profile` validation,
saved-snapshot stop_reason) were small enough to land as
fixup commits alongside the merge. The rest are tracked here.

None are invariant breaches; all are operator-experience or
robustness gaps. Group target: one PR.

## Items

### H1 — Tier-1-partial budget exhaustion reports HUNGER_EXPIRED, not BUDGET_EXHAUSTED

**Where:** `src/hungerloop/services/loop_orchestrator.py` near
the refinement hook (`_maybe_expand_or_stop_budgeted_refinement`).

**Symptom:** When tier 0 is satisfied, refinement items are
added (tier 1), and the loop budget runs out **before** tier 1
finishes, `ledger.is_done()` returns False and `HungerEngine`
emits `HUNGER_EXPIRED` at the spec §5.5 path. The post-DONE
hook never sees this case because it only fires on
`stop_reason == DONE`. Result: the user sees
`stop_reason=hunger_expired` on a task whose tier-0 correctness
already passed, which reads like a failure.

**Fix sketch:** Extend the hook to also intercept HUNGER_EXPIRED
when `ledger.tier_is_done(0)` is True. In that case, build a
BUDGET_EXHAUSTED StopReport with a recommendation string that
distinguishes "tier-0 done, refinement budget spent" from
"tier-0 not satisfied". Pin with a unit test in
`tests/unit/test_loop_orchestrator.py`.

### H2 — Empty-plan path bypasses `respect_stagnation`

**Where:** `src/hungerloop/services/loop_orchestrator.py`
`_handle_empty_plan` (around the empty-plan BLOCKED emission).

**Symptom:** `_handle_empty_plan` calls
`increment_no_progress_streak` and emits BLOCKED when
`streak >= max_global_no_progress`, regardless of
`policy.respect_stagnation`. The stagnation detector itself
honors the flag, but the empty-plan branch is a separate code
path. In current SPEND_BUDGET flow this is reachable only via
PAUSED items, so the inconsistency is rarely hit — but it's
a design hole that will bite when refinement profiles produce
items that legitimately cannot satisfy on a given loop.

**Fix sketch:**
```python
policy = self.repo.get_hunger_policy(task_id)
if policy.respect_stagnation and streak >= self.stagnation_detector.max_global_no_progress:
    return self._emit_stop(task_id, StopReason.BLOCKED)
```
Add a regression test that drives an empty plan in
`respect_stagnation=False` mode and asserts the run continues.

### H3 — Test gap: SPEND_BUDGET priority preservation

**Where:** `tests/unit/test_loop_orchestrator.py`

**Symptom:** No orchestrator-level test verifies SAFETY_STOP,
HUMAN_PAUSED, HUMAN_REQUIRED, BLOCKED still take priority over
the refinement hook in SPEND_BUDGET mode. The code is correct
(hook returns `None` when `stop_reason != DONE`), but the
invariant is unprotected against future regressions.

**Fix sketch:** Four small tests, e.g.
`test_safety_stop_wins_over_spend_budget_done`,
`test_human_paused_wins_over_spend_budget_done`, etc. Each
sets up a SPEND_BUDGET tier-0-done state, fires the higher-
priority condition, and asserts the StopReport's `stop_reason`.

### H4 — Test gap: end-to-end "all configured tiers reach DONE"

**Where:** `tests/unit/test_loop_orchestrator.py` or a new
`tests/integration/test_budgeted_refinement_e2e.py`.

**Symptom:** Spec §5.6 says: "Given all tiers from 0 through
`max_refinement_tier` are satisfied before loop budget
exhaustion, then it emits DONE." Compiler unit tests cover
tier walk; orchestrator unit tests cover BUDGET_EXHAUSTED.
Nothing drives a full task through tier 0 → tier 1 generation
→ tier 1 satisfaction → tier 2 generation → tier 2
satisfaction → DONE.

**Fix sketch:** Use `DummyModelClient.with_actions` to script
each tier's actions; assert terminal state is DONE and
StopReport has no BUDGET_EXHAUSTED traces.

### H5 — Test gap: BUDGET_EXHAUSTED recommendation string

**Where:** `tests/unit/test_stop_report_builder.py` or
`tests/unit/test_loop_orchestrator.py`.

**Symptom:** `loop_orchestrator.py` includes a specific
recommendation string ("refinement budget exhausted after
tier-0 correctness; use --refill to continue refinement or
--reset for a new run"). No test asserts the recommendation
lands in the StopReport.

**Fix sketch:** Extend the existing
`test_budgeted_mode_emits_budget_exhausted_after_base_done`
test (or its sibling) with `assert "refinement budget" in
report.recommendation.lower()`.

### H6 — `MAX_LINE_CHARS` drift between spec and implementation

**Where:** `src/hungerloop/services/context_builder.py`
(`MAX_LINE_CHARS = 500`) vs
`specs/v0.5f_implementation/v0.5f.1_context_builder.spec.md` §2.7
(`MAX_LINE_CHARS = 200`).

**Symptom:** Either the spec is wrong or the implementation is.
Pick one and align the other. No functional defect today (the
cap merely controls per-line length and the per-section caps
still hold), but a drift like this signals reviewer attention
should reset.

**Fix sketch:** Decide which value is correct (200 was a
defensive earlier number; 500 is the current implementation
choice). If 500 is correct, update the spec text. If 200 is
correct, change the constant and re-run the prompt-parity tests.

### H7 — `python_medium` profile is hardcoded, not config-driven

**Where:** `src/hungerloop/services/refinement_compiler.py`
`_PYTHON_MEDIUM_ITEMS` tuple.

**Symptom:** Spec v0.5f.4 §3.5 / FR-5 says: "The profile shall
be driven by config, not by repository guessing", with a YAML
example. The implementation hardcodes `argv=["python", "-m",
"pytest", "-q"]` etc. Spec also says "v0.5f.4 only requires
the framework and one deterministic built-in profile", so the
current state is spec-permitted. But projects that need
`pytest -xvs` or `npm test` have to fork.

**Fix sketch:** Add a `RefinementProfileLoader` that reads a
YAML profile descriptor (e.g.,
`specs/refinement_profiles/python_medium.yaml`). Keep the
hardcoded tuple as the loader's fallback for the `python_medium`
name so existing tasks behave identically. Document the YAML
schema in v0.5f.5 spec body when this lands.

## What is NOT in v0.5f.5

These are out of scope for the hardening pass and tracked
separately:

- **ReAct-in-loop / multi-tool-turn worker.** Spec v0.5f.4
  §1.3 explicitly defers this to a future
  `ExecutionWorkerV2`. v0.5f.5 does not change worker shape.
- **LLM-generated acceptance checks.** I-10 stays in force.
- **Cross-task refinement memory.** Promoted memory + skill
  cards (v0.5e) cover the long-term version.
- **Vector retrieval, RAG, embedding-based history.** Out of
  scope for v0.5f at all.

## Status

| Item | Severity | Owner | Status |
|---|---|---|---|
| H1 | Important (operator UX) | unassigned | open |
| H2 | Important (design hole) | unassigned | open |
| H3 | Important (test gap) | unassigned | open |
| H4 | Important (test gap) | unassigned | open |
| H5 | Minor (test gap) | unassigned | open |
| H6 | Minor (spec/impl drift) | unassigned | open |
| H7 | Minor (config-driven profile) | unassigned | open |

Target release: v0.5f.5. Single PR. No tag bump for v0.5f.5
unless H1+H2 ship; minor-only landings can fold into v0.5f.6.
