# HungerLoop v0.5a — Release Checklist

Pre-release verification per PRD §22.1 (v0.5a Acceptance Criteria) and §23 (Testing Plan).

## 1. Test suites

- [ ] `pytest tests/` — all unit + integration tests green (311 expected: 304 unit + 7 integration).
- [ ] `mypy --strict src/` — clean across the 60 source files.
- [ ] `ruff check src/ tests/` — no violations.
- [ ] `pytest tests/integration/` — seven end-to-end orchestrator scenarios pass.

## 2. PRD §22.1 acceptance criteria

- [ ] `hungerloop new` creates task state in the repository (CLI integration test covers in-memory; SQLite ships v0.5b).
- [ ] `hungerloop run` drives the orchestrator with `DummyModelClient`.
- [ ] Orchestrator consumes `clock.loop_count` on every accepted loop.
- [ ] Empty plan does not immediately BLOCK; stagnation detector escalates after the streak threshold.
- [ ] `LoopTrace` records `tokens_consumed_this_loop`, `cost_this_loop_usd`, `llm_calls`, `tool_calls`.
- [ ] `StopReport` supports all seven `StopReason` values (`DONE`, `HUNGER_EXPIRED`, `BLOCKED`, `SAFETY_STOP`, `HUMAN_REQUIRED`, `HUMAN_PAUSED`, `ERROR`).
- [ ] `ContextPack.budget` is `BudgetAllocation`, not `dict`.
- [ ] `RepositoryProtocol` includes every method the orchestrator calls.
- [ ] CLI `--resume` preflight blocks invalid resume attempts (`HUNGER_EXPIRED` without `--refill`, `BLOCKED` without `--unblock-all`, `SAFETY_STOP` without raised ceiling, etc.).
- [ ] All tests pass without network access.

## 3. PRD §22.2 acceptance criteria (v0.5b carry-forward verification)

The OpenAI model client landed early; verify it still satisfies the §22.2 contract:

- [ ] `OpenAIModelClient` works with `api_key_env`.
- [ ] Literal `api_key:` in YAML is rejected by `ModelConfigLoader`.
- [ ] `provider: azure_openai` raises `NotImplementedError` in v0.5a.
- [ ] `PricingTable` estimates known models; unknown models emit `unknown_model_pricing`.
- [ ] `401`/`403` becomes `HUMAN_REQUIRED` end-to-end.
- [ ] `429` honors `Retry-After`.
- [ ] LLM errors are persisted as `model_error` evidence.

## 4. Invariants (CLAUDE.md)

- [ ] I-3: Promotion requires `newly_passed_check_keys ≠ ∅`, no regressions, evidence present.
- [ ] I-4: `best/files/` empty when no commit was made (integration test `test_rejected_candidate_does_not_pollute_best`).
- [ ] I-5: `ValidationGate` re-runs previously-passed checks for regression detection.
- [ ] I-7: Every shell call goes through `SandboxRunner`; `ToolHarness` enforces side-effect levels.
- [ ] I-8: `CostGuard.assert_within_budget` is called pre and post every LLM/tool call.
- [ ] I-9: `HungerEngine.tick` checks `all_remaining_items_blocked` *before* `is_done`.

## 5. v0.5c demo verification (PRD §22.3)

- [ ] `examples/demo_task.yaml` runs to `DONE` with `DummyModelClient`.
- [ ] At least one `MemoryCandidate` is produced.
- [ ] One `SkillCard` is produced (DONE + ≥2 accepted checks).
- [ ] `hungerloop memory list <task_id>` and `hungerloop skill list` print the rows.

## 6. Documentation

- [ ] `README.md` reflects the v0.5a feature surface and test counts.
- [ ] `RELEASE_CHECKLIST.md` (this file) is up to date.
- [ ] `CLAUDE.md` invariants list is accurate.
- [ ] `hungerloop_v0_5_2_prd.md` is the canonical PRD; older PRDs are kept for history only.

## 7. Tag and ship

- [ ] Bump `pyproject.toml` `version = "0.5.0"` (or `0.5.0a`).
- [ ] `git tag v0.5a` after final verification.
- [ ] `git log --oneline 'v0.4.1..HEAD'` shows the day-by-day Day 1–14 commits without surprise diffs.

## Known gaps deferred to v0.5b+

- `SQLiteRepository` is not implemented — production CLI raises a clear `ClickException`.
- `--model-config` flag on `hungerloop run` is wired structurally but the orchestrator factory still receives the model client through `CliContext` injection in tests.
- Memory promotion (candidate → approved/rejected) and skill consumption are out of scope; `MemoryCandidate` predicates are evaluated and stored only.
