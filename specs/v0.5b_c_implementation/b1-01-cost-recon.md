# b1-01 · Cost estimate vs actual reconciliation

**Spec**: §6. **PRD**: §8.7.1. **Release**: v0.5b.1.

## Goal

When real LLM token usage diverges from PricingTable's pre-call estimate by > 20%, emit a `cost_reconciliation` event. Pure observability — does not retroactively rewrite BudgetGuard.

## Files to touch

- `src/hungerloop/services/openai_model_client.py` — post-call hook.
- `src/hungerloop/services/pricing_table.py` (already exists) — expose the estimate that fed the pre-call check (or compute it again post-call from the same prompt — pick whichever is cheaper).
- **NEW** `tests/unit/test_cost_reconciliation.py`.

## Checklist

### Threshold + env

- [ ] Module constant in `openai_model_client.py`: `_COST_DELTA_THRESHOLD_DEFAULT = 0.20`.
- [ ] On call construction, read `float(os.environ.get("HUNGERLOOP_COST_DELTA_THRESHOLD", "0.20"))`.
- [ ] Store the resolved value on the client instance so each call uses a consistent threshold.

### Reconciliation logic

- [ ] After a successful API call:
  1. Compute `estimated_tokens` from PricingTable (already done pre-call — cache that value on the call context).
  2. Read `actual_tokens` from API response (`usage.total_tokens` or computed from prompt+completion).
  3. If `estimated_tokens > 0` and `abs(actual - estimated) / estimated > threshold`: append a `cost_reconciliation` event.
- [ ] Event payload shape:
  ```python
  {
      "task_id": ..., "loop_id": ..., "model": "gpt-4o-mini",
      "estimated_tokens": 1200, "actual_tokens": 1850,
      "estimated_cost_usd": round(..., 6), "actual_cost_usd": round(..., 6),
      "delta_ratio": round((actual - estimated) / estimated, 3),
  }
  ```
- [ ] If PricingTable returned no estimate (`unknown_model_pricing` already fired): SKIP reconciliation. Don't double-warn.
- [ ] Use `EventType.COST_RECONCILIATION` (from `b0-05`).

### BudgetGuard contract (unchanged but assert)

- [ ] Add a comment in the post-call block: `# BudgetGuard.record_llm_usage already accepts the actual numbers. Reconciliation does NOT rewrite history.`
- [ ] Test that BudgetGuard's recorded usage matches the *actual*, not the estimate (existing behavior — this is a guard test).

## Tests (`test_cost_reconciliation.py`)

- [ ] `test_within_threshold_does_not_emit` — estimate=1000, actual=1100, delta=10% < 20%, no event.
- [ ] `test_over_threshold_emits_event` — estimate=1000, actual=1500, delta=50%, event with `delta_ratio=0.5`.
- [ ] `test_threshold_overridable_by_env` — set `HUNGERLOOP_COST_DELTA_THRESHOLD=0.05`, estimate=1000, actual=1100 → event.
- [ ] `test_no_estimate_skips_reconciliation` — `PricingTable` returns None → no `cost_reconciliation`, no crash.
- [ ] `test_budget_guard_records_actual_not_estimate` — even when reconciliation event fires, BudgetGuard sees the real numbers.

## Done when

- [ ] All 5 tests pass.
- [ ] `mypy --strict` clean.
- [ ] PRD §8.7.1 references this implementation.

## Notes

- Resist the temptation to "correct" BudgetGuard retroactively. Pre-call check was honest at the time; post-call divergence is data, not a bug.
- The 0.20 default is generous; ops can tighten via env per deployment.
