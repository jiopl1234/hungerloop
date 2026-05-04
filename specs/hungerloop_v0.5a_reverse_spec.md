# HungerLoop v0.5a — Reverse-Engineered Implementation Spec

**Method**: Read-and-grep across `src/hungerloop/` (~3.8k LOC, 30 services / 13 model modules / 1 repository protocol + InMemoryRepository / 14 CLI modules) and `tests/` (373 tests: 366 unit + 7 integration). Cross-referenced against `hungerloop_v0_5_2_prd.md` §22.1 acceptance criteria and `RELEASE_CHECKLIST.md`.
**Date mined**: 2026-05-04
**Source HEAD**: `b47ad23` (post-tag, with BUG-1 fix + P2/P3 coverage)
**Tag**: `v0.5a` shipped at `7164f22`; HEAD is +6 commits with two fixes and audit tests.
**Output owner**: spec-miner skill, intended as a delivery audit before v0.5b kickoff.

EARS keys: **U** = Ubiquitous, **E** = Event-driven, **S** = State-driven, **O** = Optional.

---

## 1. Technology stack

| Layer | Choice | Evidence |
|---|---|---|
| Language | Python ≥3.11, `from __future__ import annotations` everywhere | `pyproject.toml`, every module |
| Async | `asyncio`, `pytest-asyncio` auto mode | `pyproject.toml`, integration tests |
| Models | Pydantic v2 (frozen on snapshot models, `pydantic.mypy` plugin **disabled**) | `models/*.py`, `pyproject.toml` |
| CLI | `click` (groups + sub-commands) | `cli/main.py`, all `*_cmd.py` |
| HTTP | `httpx` (sync `Client`, `MockTransport` for tests) | `services/openai_model_client.py:1-50` |
| Persistence | **InMemoryRepository only** — `SQLiteRepository` is *not* implemented; schema lives in `repository/sqlite_schema.sql` (224 lines, design-only) | `repository/in_memory_repo.py`, `repository/sqlite_schema.sql:1-4` |
| Tooling | `mypy --strict`, `ruff`, `coverage` (97% line coverage at HEAD) | `pyproject.toml`, last `coverage report` |

---

## 2. Module map

```
src/hungerloop/
├── models/        # 13 frozen Pydantic v2 schemas
│   ├── enums.py        # StopReason (7 values), ValidationVerdict, AcceptanceCheckType, etc.
│   ├── hunger.py       # HungerItem/Ledger/Policy/ClockState/Snapshot, AcceptanceCheck
│   ├── blackboard.py   # BestState, CandidateState, Artifact (frozen)
│   ├── validation.py   # CheckResult, ValidationReport (frozen)
│   ├── planning.py     # Assignment, LoopPlan, BudgetAllocation
│   ├── context.py      # ContextPack(.budget: BudgetAllocation, not dict)
│   ├── tracing.py      # LoopTrace, StopReport
│   ├── worker.py       # AgentSpec, WorkerResult (with requires_human, retryable, error_type)
│   ├── memory.py       # MemoryCandidate
│   ├── skill.py        # SkillCard
│   ├── usage.py        # UsageSnapshot
│   └── workspace.py    # WorkspaceManifest
├── repository/    # Single Protocol; one in-memory implementation
│   ├── protocol.py         # ~37 abstract methods over hunger/workspace/validation/evidence/...
│   ├── in_memory_repo.py   # 432 LOC; full Protocol surface
│   └── sqlite_schema.sql   # design-only; deferred to v0.5b
├── services/      # 30 modules — all stateless wrt persistence except BudgetGuard
│   ├── hunger_engine.py    # tick() priority order I-9
│   ├── hunger_update.py    # check-level gap decrement (I-3) + EPSILON snap + malformed-key skip
│   ├── rule_based_planner.py
│   ├── agent_registry.py   # AgentSpecRegistry (code-only, no DB row in v0.5a)
│   ├── budget_allocator.py
│   ├── budget_guard.py     # PROCESS-LOCAL stateful guard (ADR-002)
│   ├── cost_guard.py       # task-ceiling; raises SafetyStopError pre+post each call
│   ├── context_builder.py
│   ├── worker_runtime.py   # thick wrapper around Worker Protocol
│   ├── execution_worker.py # the only worker shipping in v0.5a
│   ├── tools.py            # ReadFile / WriteFile / PatchFile / RunShell tools
│   ├── tool_harness.py     # policy gate (allow_shell/file_write/network) + evidence
│   ├── sandbox_runner.py   # async subprocess + timeout + process-group cleanup
│   ├── path_safety.py      # resolve_workspace_path; rejects '..' escapes
│   ├── model_client.py     # ModelClient Protocol + DummyModelClient (scripted)
│   ├── openai_model_client.py  # retry, JSON safety, Retry-After, model_error evidence
│   ├── model_config.py     # YAML loader, plaintext-key rejection
│   ├── integrator.py       # WorkerResult[] -> CandidateState
│   ├── validation_gate.py  # I-5 targeted + regression detection
│   ├── acceptance_runner.py
│   ├── commit_manager.py   # check-level promotion (I-3); BestState.score frozen at 0
│   ├── workspace_manager.py # candidates/loop_NNN/ -> best/ with atomic rename (BUG-1 fix)
│   ├── stagnation_detector.py # attempted-only (I-6)
│   ├── stop_report_builder.py
│   ├── memory_manager.py   # propose_from_loop with deterministic predicates
│   ├── skill_manager.py    # maybe_create_skill_card on DONE + ≥2 accepted checks
│   ├── loop_orchestrator.py # the v0.5a marquee component
│   └── requirement_compiler.py
└── cli/           # 14 click modules
    ├── main.py             # version, group, default context (raises until SQLiteRepository ships)
    ├── context.py          # CliContext(repo, workspace_root, model_client?)
    ├── new_cmd.py          # `hungerloop new <goal> --accept <json>`
    ├── run_cmd.py          # `hungerloop run <task_id> [--refill / --resume / --raise-cost-ceiling / --max-loops]`
    ├── status_cmd.py       # `hungerloop status <task_id>`
    ├── status_format.py
    ├── hunger_cmd.py       # `hungerloop hunger {refill,unblock,unblock-all,freeze,resume}`
    ├── memory_cmd.py       # `hungerloop memory list <task_id>`
    ├── skill_cmd.py        # `hungerloop skill list`
    ├── workspace_cmd.py    # `hungerloop workspace {best,candidate,rejected}` (filesystem inspectors)
    ├── checks_cmd.py       # `hungerloop checks <task_id>` (sqlite inspector)
    ├── preflight.py        # check_resume_preflight: gates --resume scenarios
    └── orchestrator_factory.py # build_orchestrator wires services from CliContext
```

---

## 3. Observed Requirements (EARS)

### 3.1 Stop-reason routing (HungerEngine)

- **U-SR-01** The system shall expose seven `StopReason` values: `DONE`, `HUNGER_EXPIRED`, `BLOCKED`, `HUMAN_REQUIRED`, `HUMAN_PAUSED`, `SAFETY_STOP`, `ERROR`. *(`models/enums.py:4-11`)*
- **E-SR-02** When `clock.frozen` is true at tick time, the engine shall emit `HUMAN_PAUSED` ahead of any other reason. *(`services/hunger_engine.py:65-67`)*
- **E-SR-03** When `consumed_by_cost_usd ≥ max_total_cost_usd` or `consumed_tokens ≥ max_total_tokens`, the engine shall emit `SAFETY_STOP`. *(`services/hunger_engine.py:69-75`)*
- **E-SR-04** When every unfinished item is BLOCKED, the engine shall emit `BLOCKED` *before* checking `is_done()` (I-9). *(`services/hunger_engine.py:77-79`, `tests/unit/test_hunger_engine.py:test_tick_priority_blocked_beats_hunger_expired`)*
- **E-SR-05** When `drive_budget ≤ 0` and the ledger is not done, the engine shall emit `HUNGER_EXPIRED`; if the ledger *is* done, it shall emit `DONE`. *(`services/hunger_engine.py:81-87`)*
- **U-SR-06** Stop-reason priority shall be exactly `HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`; verified by 5 priority-order tests. *(`tests/unit/test_hunger_engine.py:test_tick_priority_*`)*

### 3.2 Loop orchestration

- **U-LO-01** `LoopOrchestrator.step(task_id)` shall return `LoopTrace | StopReport` and never raise; `SafetyStopError` from CostGuard is converted to `StopReason.SAFETY_STOP`. *(`services/loop_orchestrator.py:109-167`)*
- **E-LO-02** When the snapshot reports `should_stop`, the orchestrator shall short-circuit before incrementing `loop_count` or creating a candidate workspace. *(`services/loop_orchestrator.py:127-129`)*
- **U-LO-03** On every accepted loop, the orchestrator shall (a) call `next_loop_id`, (b) increment and persist `clock.loop_count` *before* dispatching to the worker (NFR N1 recoverability), (c) create the candidate workspace, (d) snapshot usage. *(`services/loop_orchestrator.py:131-143`)*
- **E-LO-04** When `LoopPlan.assignments` is empty, the orchestrator shall reject the candidate, increment the no-progress streak, and emit `BLOCKED` only after the stagnation threshold is crossed (M5: empty plan ≠ immediate BLOCKED). *(`services/loop_orchestrator.py:149-155`, `services/loop_orchestrator.py:279-310`)*
- **E-LO-05** When any `WorkerResult.requires_human` is true, the orchestrator shall reject the candidate and emit `HUMAN_REQUIRED`. *(`services/loop_orchestrator.py:169-171`)*
- **U-LO-06** The orchestrator shall persist `LoopTrace` for both committed and rejected loops including delta usage (`tokens_consumed_this_loop`, `cost_this_loop_usd`, `llm_calls`, `tool_calls`). *(`services/loop_orchestrator.py:194-222`)*
- **U-LO-07** The orchestrator shall *not* persist `StopReport`. The CLI is the sole writer (M4 / §28.16). *(`services/loop_orchestrator.py:312-329`, `cli/run_cmd.py`)*
- **S-LO-08** While `max_loops_safety_cap` (default 1000) is not reached, `run` shall keep calling `step`; on cap exhaustion it emits `StopReason.ERROR` with a recommendation. *(`services/loop_orchestrator.py:228-246`)*

### 3.3 Hunger update (I-3, BRITTLE-1)

- **E-HU-01** When the validation verdict is `PASS` or `PARTIAL`, the service shall decrement `gap_score` proportionally to newly-passed check count divided by total checks. *(`services/hunger_update.py:40-59`)*
- **E-HU-02** When `gap_score ≤ EPSILON` (1e-9) after decrement, the service shall snap to `0.0` and flip status to `VALIDATED_SATISFIED`. *(`services/hunger_update.py:60-67`)*
- **E-HU-03** When the item appears in `satisfied_hunger_item_ids`, gap is force-zeroed regardless of decrement size. *(`services/hunger_update.py:56-57`)*
- **E-HU-04** When a `newly_passed_check_keys` entry lacks the `:` separator, the service shall log a warning and skip the entry without raising (BRITTLE-1 fix). *(`services/hunger_update.py:48-58`, `tests/unit/test_hunger_update.py:test_malformed_check_key_is_skipped_not_crashing`)*
- **E-HU-05** When verdict is `FAIL`, the service shall be a no-op — no item writes occur. *(`services/hunger_update.py:40-41`)*

### 3.4 Workspace management (BUG-1 fix)

- **U-WS-01** `WorkspaceManager` shall be the only mover of `candidates/loop_NNN/files/` into `best/files/`. *(`services/workspace_manager.py`)*
- **E-WS-02** When `promote_candidate_to_best` runs, it shall (a) stage the new content into `.best.staging.<pid>.<uuid>/`, (b) atomically rename existing `best/` to `.best.old.<token>`, (c) atomically rename staging to `best/`, (d) on rename failure, restore the old via reverse rename. *(`services/workspace_manager.py:promote_candidate_to_best`, `tests/unit/test_workspace_manager.py:test_two_consecutive_failures_do_not_destroy_original`)*
- **E-WS-03** When `copytree` to staging fails, `best/` shall remain untouched. *(`tests/unit/test_workspace_manager.py:test_promote_copytree_failure_leaves_best_intact`)*
- **U-WS-04** Token uniqueness (`pid + uuid[:8]`) shall prevent concurrent failed promotes from clobbering each other's recovery state (BUG-1 regression net). *(`tests/unit/test_workspace_manager.py:test_two_consecutive_failures_do_not_destroy_original`)*

### 3.5 Validation gate (I-5)

- **U-VG-01** `ValidationGate.validate` shall execute (a) all checks of every target item plus (b) all previously-passed checks even from non-target items. *(`services/validation_gate.py:64-94`, `tests/unit/test_validation_gate.py:test_previously_passed_check_not_targeted_stays_passed`)*
- **E-VG-02** When a previously-passed check fails on the candidate, the gate shall mark the check key as `regressed` and force the verdict to `FAIL`. *(`services/validation_gate.py:96-97,210-220`, `tests/unit/test_validation_gate.py:test_regression_against_baseline_emits_fail`)*
- **E-VG-03** When the candidate produces zero evidence (no `evidence_ids`, no shell-emitting checks), the verdict shall be `FAIL` with `missing_evidence` populated. *(`services/validation_gate.py:130-132`, `tests/unit/test_validation_gate.py:test_no_evidence_anywhere_emits_fail_with_missing_evidence`)*
- **U-VG-04** `currently_passed_check_keys` shall be the union of newly-passed checks and previously-passed checks that were *not* re-run; untested checks stay passed. *(`services/validation_gate.py:116-120`)*
- **U-VG-05** Item satisfaction shall obey `acceptance_mode`: `"all"` requires every check to pass, `"any"` requires ≥1. *(`services/validation_gate.py:177-208`, `tests/unit/test_validation_gate.py:test_acceptance_mode_any_satisfied_when_one_check_passes`)*
- **E-VG-06** When a target item has zero acceptance checks, it shall land in `unsatisfied_hunger_item_ids` (not satisfied, not skipped). *(`tests/unit/test_validation_gate.py:test_target_item_with_no_checks_is_unsatisfied`)*

### 3.6 Cost & budget (I-8, M12, ADR-002)

- **U-CG-01** Every LLM and tool call shall be wrapped by `CostGuard.assert_within_budget` *both before and after* the call; on breach, `SafetyStopError` is raised. *(`services/cost_guard.py`, `services/openai_model_client.py`, `services/tool_harness.py`)*
- **U-BG-01** `BudgetGuard` shall hold per-(task, loop, agent) usage in process memory and raise `WorkerBudgetExceeded` when `(addl_llm_calls, addl_tool_calls, addl_tokens, addl_wall_clock_s)` would breach `BudgetAllocation`. *(`services/budget_guard.py`, ADR-002)*
- **U-BG-02** `ContextPack.budget` shall be a `BudgetAllocation` instance, not a `dict`. *(`models/context.py:38`)*
- **E-BG-03** When `WorkerResult.error_type == "worker_budget_exceeded"`, the loop shall be counted by stagnation but not abort the run. *(`services/loop_orchestrator.py`, `services/stagnation_detector.py`)*

### 3.7 Tool harness & sandbox (I-7, ADR-003)

- **U-TH-01** ToolHarness shall enforce `BudgetAllocation.allow_shell / allow_file_write / allow_network` before dispatching the tool. *(`services/tool_harness.py`)*
- **E-TH-02** When a tool raises `ValueError` or `PermissionError`, ToolHarness shall convert to a `ToolResult` with `error_type ∈ {"invalid_args"}` and emit a `tool_call` evidence row marked `success=False`. *(`services/tool_harness.py`, `tests/unit/test_tool_harness.py:test_path_safety_violation_surfaces_as_invalid_args`)*
- **E-TH-03** When a tool raises any other exception, ToolHarness shall wrap it as `error_type="tool_exception"` with the same evidence contract. *(`services/tool_harness.py`)*
- **U-TH-04** `SandboxRunner` shall be the only entry point that calls `subprocess`; all shell tools route through it. *(`services/sandbox_runner.py`, ADR-003)*
- **U-TH-05** All filesystem paths from tool params shall be resolved through `path_safety.resolve_workspace_path`, which rejects `..`-escapes outside the candidate workspace. *(`services/path_safety.py`, `tests/unit/test_path_safety.py`)*

### 3.8 Model client (ADR-004)

- **U-MC-01** `ModelClient` Protocol shall expose `complete_json(...)` returning `ModelResponse(actions=[...], content?, tokens_in?, tokens_out?, cost_usd?)`. *(`services/model_client.py`)*
- **E-MC-02** When the OpenAI client receives 401/403, it shall raise `ModelAuthError(retryable=False)` and the worker shall return `requires_human=True` (→ `HUMAN_REQUIRED`). *(`services/openai_model_client.py`)*
- **E-MC-03** When the OpenAI client receives 429 with `Retry-After`, it shall sleep for the indicated seconds; RFC1123 HTTP-date format is parsed via `email.utils.parsedate_to_datetime`. *(`services/openai_model_client.py`)*
- **E-MC-04** When `httpx.TransportError` occurs, the client shall retry up to `max_retries` and emit a `model_error` evidence row carrying `error_type` and `retryable`. *(`services/openai_model_client.py`, ADR-004)*
- **E-MC-05** When the response body is not valid JSON or not a dict, the client shall raise `ModelCallError` rather than crashing on `KeyError`. *(`services/openai_model_client.py`, M7 / §28.3)*
- **U-MC-06** `DummyModelClient.with_actions([...])` shall be scripted-deterministic; the demo and integration tests use it so no network is required. *(`services/model_client.py:97-...`, M21 / §28.1)*
- **E-MC-07** When a YAML model config contains a literal `api_key:` field, `ModelConfigLoader` shall reject it; only `api_key_env: ENV_VAR` is accepted. *(`services/model_config.py`, §10.1)*
- **E-MC-08** When `provider: azure_openai` appears in v0.5a, the loader shall raise `NotImplementedError` (Azure deferred to v0.5b). *(`services/model_config.py`)*

### 3.9 CLI surface (PRD §18)

- **U-CLI-01** `hungerloop new <goal> --accept <json>` shall create a task with a compiled hunger ledger; `--task-id` is optional. *(`cli/new_cmd.py`, `tests/unit/test_cli_commands.py:test_new_creates_task_with_acceptance_check`)*
- **E-CLI-02** When `--accept` is missing, `new` shall exit non-zero with a message naming the missing flag. *(`tests/unit/test_cli_commands.py:test_new_requires_at_least_one_accept`)*
- **E-CLI-03** When `--accept` is not valid JSON, `new` shall exit non-zero with "valid JSON" in the message. *(`tests/unit/test_cli_commands.py:test_new_rejects_invalid_json`)*
- **U-CLI-04** `hungerloop run <task_id>` shall (a) preflight against the last `StopReason`, (b) apply `--refill / --raise-cost-ceiling / --resume` mutations *before* the orchestrator runs, (c) drive the orchestrator until a `StopReport`, (d) persist the `StopReport` (M4). *(`cli/run_cmd.py`, `cli/preflight.py`)*
- **E-CLI-05** When the previous stop was `HUNGER_EXPIRED` and `--refill` is not provided (or is ≤0), `run` shall fail preflight with exit code 2 and a printed remediation. *(`cli/preflight.py:67-73`, `tests/unit/test_cli_commands.py:test_run_preflight_blocks_on_hunger_expired_without_refill`)*
- **E-CLI-06** When the previous stop was `BLOCKED` and neither `--unblock-all` nor any open items exist, preflight shall reject. *(`cli/preflight.py:75-81`)*
- **E-CLI-07** When the previous stop was `SAFETY_STOP`, `--raise-cost-ceiling <USD>` must exceed the current ceiling; the new ceiling is applied *before* preflight is re-evaluated. *(`cli/preflight.py:91-102`, `tests/unit/test_cli_commands.py:test_run_raise_cost_ceiling_updates_policy`)*
- **E-CLI-08** When `--resume` is passed and the clock is `frozen`, the CLI shall flip `clock.frozen=False` and append a `hunger_resumed` event. *(`cli/run_cmd.py`, `tests/unit/test_cli_commands.py:test_run_resume_unfreezes_clock_and_emits_event`)*
- **E-CLI-09** When `--resume` is passed and the clock is *already* unfrozen, no `hunger_resumed` event shall be emitted (idempotent no-op). *(`tests/unit/test_cli_commands.py:test_run_resume_on_unfrozen_clock_is_idempotent`)*
- **U-CLI-10** `hungerloop hunger refill <task_id> --loops N` shall **decrement** `clock.loop_count` by `N` (not reset to 0); requires `N > 0` (M13). *(`cli/hunger_cmd.py`, `tests/unit/test_cli_commands.py:test_hunger_refill_decrements_loop_count`, `test_hunger_refill_rejects_zero`)*
- **U-CLI-11** `hungerloop hunger {unblock,unblock-all,freeze,resume}` shall mutate hunger state out-of-band of the orchestrator. *(`cli/hunger_cmd.py`, `tests/unit/test_cli_commands.py`)*
- **U-CLI-12** `hungerloop status <task_id>` shall print task summary without mutation. *(`cli/status_cmd.py`)*
- **U-CLI-13** `hungerloop memory list <task_id>` and `hungerloop skill list` shall enumerate stored candidates and skill cards. *(`cli/memory_cmd.py`, `cli/skill_cmd.py`)*
- **U-CLI-14** `hungerloop workspace {best,candidate,rejected}` shall list files+sizes from disk; missing directories produce friendly messages. *(`cli/workspace_cmd.py`, `tests/unit/test_cli_workspace_checks.py`)*
- **E-CLI-15** When the default context factory is invoked without test injection, `hungerloop` shall raise `ClickException("v0.5a CLI cannot run without SQLiteRepository yet…")` — a deliberate guard until v0.5b. *(`cli/main.py:41-53`, `tests/unit/test_cli_commands.py:test_default_main_without_context_raises_clear_error`)*

### 3.10 Memory & skill (PRD §19, §20)

- **U-MS-01** `MemoryManager.propose_from_loop` shall produce at most one `MemoryCandidate` per loop and only if all four predicates hold (`action_verified`, `reusable`, `non_volatile`, `traceable`). *(`services/memory_manager.py`, `tests/unit/test_memory_manager.py`)*
- **U-MS-02** `SkillManager.maybe_create_skill_card` shall emit a `SkillCard` only when `stop_reason == DONE` *and* accepted check count ≥ 2; called by the CLI after the StopReport is built. *(`services/skill_manager.py`, `tests/integration/test_skill_card_trigger.py`)*

### 3.11 Repository & persistence (NFR N1)

- **U-RP-01** `RepositoryProtocol` shall expose every method the orchestrator and CLI need; no service shall hold `repo: Any` (M10 closed — verified by grep returning empty). *(`repository/protocol.py`, grep for `repo: Any` empty)*
- **U-RP-02** `InMemoryRepository` shall implement the entire Protocol surface; tests and the demo run against it. *(`repository/in_memory_repo.py:33-432`)*
- **U-RP-03** `StopReport` history shall accumulate (M16): each `save_stop_report` appends to `_stop_reports_history[task_id]`. *(`repository/in_memory_repo.py:358-363`)*
- **U-RP-04** `append_event(event_type, payload, task_id?, loop_id?)` shall include `task_id`/`loop_id` columns (M15). *(`repository/in_memory_repo.py:368-383`)*
- **U-RP-05** `save_tool_call_as_evidence` is part of the protocol (M18). *(`repository/protocol.py`, `repository/in_memory_repo.py:281-307`)*

### 3.12 Integration scenarios (tests/integration/)

- **E-IT-01** End-to-end DummyModelClient run with a `file_exists` check shall reach `DONE` and persist a `StopReport`. *(`tests/integration/test_orchestrator_dummy_done.py`)*
- **E-IT-02** A 401 response from a stubbed OpenAI client shall route through to `HUMAN_REQUIRED`. *(`tests/integration/test_orchestrator_human_required.py`)*
- **E-IT-03** A task whose policy budget is exhausted before progress shall stop with `HUNGER_EXPIRED`. *(`tests/integration/test_orchestrator_hunger_expired.py`)*
- **E-IT-04** A task whose cost ceiling is breached mid-call shall stop with `SAFETY_STOP`. *(`tests/integration/test_orchestrator_safety_stop.py`)*
- **E-IT-05** A rejected candidate (validation FAIL, regression, or empty evidence) shall not pollute `best/files/` (I-4). *(`tests/integration/test_rejected_candidate_does_not_pollute_best.py`)*
- **E-IT-06** A run that ends in DONE with ≥2 accepted checks shall produce one `SkillCard`. *(`tests/integration/test_skill_card_trigger.py`)*

---

## 4. Non-functional observations

| NFR | Status at HEAD | Evidence |
|---|---|---|
| **N1 Recoverability** | Partial — `loop_count++` is persisted before worker dispatch, but production persistence (SQLite WAL) ships v0.5b. | `services/loop_orchestrator.py:134-135`; `RELEASE_CHECKLIST.md:68` |
| **N2 Workspace isolation** | Fully enforced — atomic promote (BUG-1 fixed), path_safety on every tool param, ToolHarness gates side-effects. | `services/workspace_manager.py`, `services/path_safety.py`, `services/tool_harness.py` |
| **N3 Cost containment** | Two-tier active: `CostGuard` (task) + `BudgetGuard` (loop/worker, in-memory). | `services/cost_guard.py`, `services/budget_guard.py`, ADR-002 |
| **N4 Observability** | `LoopTrace` + `StopReport` history + typed Evidence enum + events with task_id/loop_id. | `models/tracing.py`, `repository/in_memory_repo.py:358-383` |
| **N5 Type safety** | `mypy --strict` clean across 60 source files; `repo: Any` count = 0. | `pyproject.toml`, last `mypy --strict` run |
| **N6 Testability without network** | All 373 tests run without network (DummyModelClient + httpx.MockTransport). | Last `pytest tests/` run |
| **N7 Coverage** | 97% line coverage at HEAD; HungerEngine 97%, ValidationGate 96%, CLI inspectors ~100%. | Last `coverage report` |

---

## 5. Inferred acceptance criteria (PRD §22.1 vs. observed)

| PRD criterion | Observed | Notes |
|---|---|---|
| `hungerloop new` creates task state | ✅ | InMemoryRepository in tests; SQLiteRepository deferred to v0.5b. |
| `hungerloop run` with DummyModelClient | ✅ | `tests/integration/test_orchestrator_dummy_done.py` end-to-end. |
| Orchestrator consumes `clock.loop_count` per accepted loop | ✅ | `services/loop_orchestrator.py:134-135` writes before dispatch. |
| Empty plan does not immediately BLOCK | ✅ | M5 path verified at `services/loop_orchestrator.py:279-310`. |
| `LoopTrace` records tokens/cost/llm_calls/tool_calls | ✅ | `services/loop_orchestrator.py:210-213`. |
| `StopReport` supports all 7 StopReason values | ✅ | Enum at `models/enums.py:4-11`; integration tests cover DONE / HUNGER_EXPIRED / SAFETY_STOP / HUMAN_REQUIRED / BLOCKED. |
| `ContextPack.budget` is `BudgetAllocation` | ✅ | `models/context.py:38`. |
| `RepositoryProtocol` includes all orchestrator methods | ✅ | Grep for `repo: Any` empty; M10 closed. |
| `--resume` preflight blocks invalid attempts | ✅ | `cli/preflight.py` covers HUNGER_EXPIRED / BLOCKED / HUMAN_REQUIRED / SAFETY_STOP / HUMAN_PAUSED. |
| Tests pass without network | ✅ | 373/373 green offline. |

**v0.5b carry-forwards (PRD §22.2) already shipped early in v0.5a** — confirmed by `RELEASE_CHECKLIST.md` §3 and `services/openai_model_client.py`:
- `OpenAIModelClient` works with `api_key_env`. ✅
- Literal `api_key:` rejected. ✅
- `azure_openai` raises `NotImplementedError`. ✅
- `PricingTable` for known/unknown models. ✅
- `401`/`403` → `HUMAN_REQUIRED` end-to-end. ✅
- `429` honors `Retry-After` (RFC1123 + numeric). ✅
- `model_error` evidence persisted on LLM errors. ✅

---

## 6. Uncertainties & open questions

| ID | Question | Why it matters |
|---|---|---|
| **U1** | `SQLiteRepository` is not implemented; production CLI always raises. | The README and PRD §22.1 imply persistence is part of v0.5a (criterion 1 says "creates task state in SQLite"). `RELEASE_CHECKLIST.md:68` openly defers it to v0.5b. Either the PRD criterion or the release checklist is the source of truth — they currently disagree. |
| **U2** | `BestState.score` is preserved in the schema but frozen at 0. | I-3 forbids score-based commits; the field exists for forward-compat but is never set. Future readers may misread it as live state. Marker comment exists in `services/commit_manager.py` but a `Field(deprecated=True)` annotation could be clearer. |
| **U3** | `BudgetGuard` is in-memory only (ADR-002). | If a process crashes mid-loop, partial token/cost spend is unaudited. v0.5a says this is acceptable because `loop_count` is consumed at the start of the loop (counted as spent regardless), but this premise breaks if v0.5b adds intra-loop checkpoints. |
| **U4** | `StopReport` persistence ownership is split between Orchestrator (build) and CLI (save). | M4 / §28.16 documents this, but a non-CLI caller (e.g. an SDK harness) could forget. Worth a dedicated `OrchestratorRunner` helper in v0.5b. |
| **U5** | `events.jsonl` is mentioned in PRD §18.1 but `overview.md §8` declares it removed in favor of the SQLite `events` table. | The schema file has an `events` table; nothing writes a JSONL stream. Code matches the resolution; PRD wording should be cleaned up. |
| **U6** | `set_hunger_policy` and `grant_approval` are NOT in `RepositoryProtocol` but are on `InMemoryRepository`. | The in-memory repo declares them as test/CLI helpers (`# Test/CLI-only setup helper (not in protocol; per reverse-spec U6)` at `repository/in_memory_repo.py:88`). When SQLiteRepository lands these need real implementations or explicit "not part of protocol" enforcement at the type level. |
| **U7** | `HungerClockState` has no `task_id` field. `save_hunger_clock` resolves the owner by `is`-identity walk over the dict. | This is fine for InMemory because Pydantic returns the same instance, but for SQLiteRepository the round-trip will produce a fresh object and the `is` check will silently no-op. Schema needs a `task_id` column on `hunger_clocks` (already in the SQL file) and the model probably should too. |
| **U8** | `LLM_JUDGE` acceptance check raises `NotImplementedError`. | Documented as deferred (V1.2+) but not gated at config time — a YAML containing `LLM_JUDGE` would compile to a ledger that crashes at validation time, not at task creation. |

---

## 6.1 Allocation decision (2026-05-04)

After reviewing against `hungerloop_v0_5b_c_prd.md`, the eight uncertainties were allocated as follows:

| # | Allocated to | Reason |
|---|---|---|
| **U7** save_hunger_clock signature | **v0.5b.0 Day 1** (PRD §1.1.1 D1-A) | Breaking change in §4.1 protocol; must run before `SQLiteRepository` consumes the new signature |
| **U4** StopReport ownership swap | **v0.5b.0 Day 1** (PRD §1.1.1 D1-B) | §12.0 pipeline already moves persistence into the Orchestrator; do the deletion before SQLite tests are written against the new path |
| **U6** save_hunger_policy rename | **v0.5b.0 Day 1** (PRD §1.1.1 D1-C) | §4.1 line 696 already declares the new name; mechanical rename bundled with U7 |
| **U8** LLM_JUDGE compile-time reject | **v0.5b.0 Day 1** (PRD §1.1.1 D1-D) | 10 LOC ergonomic guard; once SQLite ships, deferred-to-validation failures become expensive to diagnose post-mortem |
| **U1** SQLiteRepository | **v0.5b.0 core scope** (PRD §1.1, §5) | Already on the schedule |
| **U2** BestState.score deprecated marker | **bundle into any model-touch day** | Cosmetic; non-blocking |
| **U5** v0.5a PRD `events.jsonl` residue | **separate v0.5a doc PR** | Pure doc cleanup; v0.5b/c PRD has no `jsonl` reference |
| **U3** BudgetGuard process-local | **no action** | §2.1 explicitly preserves the v0.5a context-based API; ADR-002 is the authoritative record |

---

## 7. Recommendations

1. **Reconcile PRD §22.1 ↔ release checklist on SQLite.** Either (a) acknowledge in PRD that "creates task state in SQLite" slipped to v0.5b, or (b) add a thin SQLiteRepository pass before tagging v0.5b. The current state — schema + tests + CLI guard but no impl — is honest but easy to miss.
2. **Promote `set_hunger_policy` & `grant_approval` to the Protocol** (or add a separate `RepositoryAdminProtocol`) so SQLiteRepository inherits a typed contract instead of a comment.
3. **Add `task_id` to `HungerClockState`**. Cheap now; expensive once SQLiteRepository ships and the `is`-identity branch starts dropping writes silently.
4. **Reject `LLM_JUDGE` at task-compile time** (`requirement_compiler.py`) with a clear "deferred to V1.2" message. Today it fails at validation, which is one full loop of wasted budget.
5. **Document `BestState.score` as deprecated-not-removed** via Pydantic field metadata, so editor tooltips warn instead of inviting future re-use.
6. **Wrap `Orchestrator.step + StopReport persistence` into a runner helper** so non-CLI callers can't accidentally drop the StopReport (M4 contract today is a doc, not code).
7. **Tighten BudgetGuard's process-locality contract** — when v0.5b adds intra-loop checkpoint resume, this guard needs either persistence or an explicit "loop is restarted from scratch" rule. ADR-002 is the right place to update.
8. **Strip `events.jsonl` references from PRD §18.1** to match the resolved decision in `overview.md §8`. Single source of truth across docs.

---

## 8. Delivery summary (v0.5a)

| Metric | Value |
|---|---|
| Tag | `v0.5a` (commit `7164f22`) |
| HEAD over tag | +6 commits (BUG-1 fix, audit tests, BRITTLE-1 fix, P2/P3 coverage) |
| Source files | 60 Python modules under `src/hungerloop/` |
| Tests | 373 (366 unit + 7 integration), all green offline |
| Coverage | 97% line |
| `mypy --strict` | clean across 60 files |
| `ruff check` | clean |
| ADRs | 6 (`docs/architecture/v0.5a/adr/`) |
| PRD §22.1 criteria met | 10/10 (with SQLite caveat U1) |
| PRD §22.2 carry-forwards | 7/7 (OpenAI client landed early) |
| Known deferrals | SQLiteRepository, `--model-config` runtime wire-up, memory promotion |

**Bottom line**: v0.5a met every PRD §22.1 acceptance criterion at the InMemoryRepository level, plus seven §22.2 OpenAI items as a bonus. The single honest gap is the absence of SQLiteRepository; the schema, the CLI guard, and the release checklist all point to v0.5b. The +6 post-tag commits hardened two real-world failure modes (atomic promotion, malformed check_keys) and pushed line coverage from 92% to 97%.
