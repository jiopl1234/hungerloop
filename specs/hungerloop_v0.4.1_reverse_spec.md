# HungerLoop v0.4.1 — Reverse-Engineered Specification

**Method**: Read-and-grep across `src/hungerloop/` (~2.1k LOC, 14 services / 11 models / 2 repositories / 3 CLI files) and `tests/unit/` (89 tests, 1.5k LOC).
**Date mined**: 2026-05-02
**Source SHA**: `5f7435c` (last commit before v0.5a work)
**Output owner**: produced by spec-miner skill, intended as a behavioral baseline for v0.5a refactors.

EARS format keys: **U** = Ubiquitous, **E** = Event-driven, **S** = State-driven, **O** = Optional.
Each requirement carries a code citation `path:line` so reviewers can verify.

---

## 1. Technology Stack & Architecture

### 1.1 Stack
- **Language**: Python ≥3.11 (pyproject.toml:9). All modules use `from __future__ import annotations`.
- **Models**: Pydantic v2 (`pydantic>=2.0,<3.0`). The `pydantic.mypy` plugin is intentionally disabled (pyproject.toml:33-36 — incompatible with mypy ≥1.18).
- **CLI**: Click v8 (pyproject.toml:12).
- **Concurrency**: `asyncio` (subprocess + `asyncio.wait_for`) for sandbox execution and validation.
- **Persistence**: In-memory only (`InMemoryRepository`). No SQLite/Postgres/file persistence in v0.4.1 despite `cli/checks_cmd.py` referencing `blackboard.sqlite` (see §6 uncertainty U1).
- **Testing**: pytest v8 + `pytest-asyncio` in `auto` mode. 89 tests, all unit (no integration directory populated despite `tests/integration/` existing).
- **Type checking**: `mypy --strict` (pyproject.toml:30-32).
- **Lint/format**: `ruff` with `E,F,I` rules; line-length 100.

### 1.2 Architecture style
- Service-oriented monolith inside one Python package.
- Layered: **CLI** → **Services** → **Repository protocol** → **InMemoryRepository**.
- Models are *data-only* Pydantic classes (`BestState`, `CandidateState`, `CheckResult`, `ValidationReport` are `frozen=True`); business logic lives in services.
- Dependency injection by constructor: every service that touches persistence takes `repo: Any` (a `RepositoryProtocol` typed with `Any` per `TODO(Task 14)` markers in 6 services).
- Async only where I/O demands it: sandbox subprocess execution and the validation gate. Hunger evaluation, commit decisions, hunger updates, stagnation tracking are synchronous.

### 1.3 Non-features (deliberately deferred)
- No orchestrator (`LoopOrchestrator` does not exist).
- No worker runtime (`Worker` is a Pydantic spec only; no runtime invocation).
- No model client (`ModelClient` does not exist).
- No tool harness; workers cannot execute anything.
- `LLM_JUDGE` acceptance check raises `NotImplementedError` (acceptance_runner.py:108-111).
- `STAGE_BASED` decay is implemented as linear (hunger_engine.py:138-146).

---

## 2. Module & Directory Structure

```
src/hungerloop/
├── __init__.py                       (empty)
├── models/                           (data classes)
│   ├── enums.py        StopReason, ValidationVerdict, AcceptanceCheckType,
│   │                   HungerItemStatus, HungerItemType, DecayType, LoopPhase
│   ├── hunger.py       AcceptanceCheck, HungerItem, HungerLedger,
│   │                   HungerPolicy, HungerClockState, HungerSnapshot
│   ├── blackboard.py   BestState (frozen), CandidateState (frozen)
│   ├── validation.py   CheckResult (frozen), ValidationReport (frozen)
│   ├── planning.py     Assignment, LoopPlan, BudgetAllocation
│   ├── tracing.py      LoopTrace, StopReport
│   ├── context.py      ContextPack
│   ├── worker.py       AgentSpec, WorkerResult
│   └── workspace.py    WorkspaceManifest, WorkspaceStatus literal
├── services/                         (stateless behavior)
│   ├── hunger_engine.py        HungerEngine.tick()
│   ├── hunger_update.py        HungerUpdateService.apply_validation()
│   ├── cost_guard.py           CostGuard, SafetyStopError
│   ├── validation_gate.py      ValidationGate.validate(), make_check_key()
│   ├── commit_manager.py       CommitManager.apply()
│   ├── integrator.py           Integrator.integrate()
│   ├── context_builder.py      ContextBuilder.build_for_agent()
│   ├── workspace_manager.py    WorkspaceManager (filesystem)
│   ├── sandbox_runner.py       SandboxRunner, SandboxRunResult
│   ├── acceptance_runner.py    AcceptanceCheckRunner (dispatch by check_type)
│   ├── stagnation_detector.py  StagnationDetector.update()
│   ├── requirement_compiler.py RuleBasedCompiler.compile()
│   └── path_safety.py          resolve_workspace_path()
├── repository/
│   ├── protocol.py       RepositoryProtocol (~22 methods)
│   └── in_memory_repo.py InMemoryRepository (also exposes 3 setters
│                         not in Protocol — see §6 U6)
└── cli/
    ├── main.py            cli (click group, version 0.4.1)
    ├── workspace_cmd.py   workspace best/candidate/rejected
    └── checks_cmd.py      checks (reads SQLite that doesn't exist)
```

---

## 3. Observed Requirements (EARS)

### 3.1 Hunger ledger — `models/hunger.py`

| ID | EARS | Citation |
|---|---|---|
| **R-HL-001** | **U** The `HungerLedger` shall expose `active_items()` returning items whose `status` ∉ {CLOSED, PAUSED, BLOCKED} **and** whose `gap_score > 0`. | hunger.py:84-90 |
| **R-HL-002** | **U** The `HungerLedger` shall expose `blocked_items()` returning items whose `status == BLOCKED` and `gap_score > 0`. | hunger.py:92-97 |
| **R-HL-003** | **U** The `HungerLedger` shall expose `unfinished_items()` returning items whose `status` ∉ {CLOSED, VALIDATED_SATISFIED} and `gap_score > 0`. | hunger.py:99-105 |
| **R-HL-004** | **U** The `HungerLedger.work_pressure()` shall return `Σ priority × gap_score` over `active_items()` only. BLOCKED and PAUSED items contribute zero. | hunger.py:107-108; test_blocked_semantics.py:63-71 |
| **R-HL-005** | **U** `HungerLedger.is_done()` shall return `not unfinished_items()`. An empty ledger is therefore done. | hunger.py:122-123; test_blocked_semantics.py:74-77 |
| **R-HL-006** | **U** `HungerLedger.all_remaining_items_blocked()` shall return `True` only when `unfinished_items()` is non-empty AND all of them are BLOCKED. An empty ledger returns `False`. | hunger.py:116-120; test_blocked_semantics.py:74-77 |
| **R-HL-007** | **S** While an item's `status` is BLOCKED, `is_done()` shall remain `False` even though `work_pressure()` is 0. (Encodes I-9: BLOCKED ≠ DONE.) | hunger.py:99-105 + test_loop_count_decay.py:95-106 |
| **R-HI-001** | **U** A `HungerItem` shall default to `priority=1.0`, `gap_score=1.0`, `status=OPEN`, `acceptance_mode="all"`, `consecutive_failure_count=0`. | hunger.py:45-59 |

### 3.2 Hunger engine — `services/hunger_engine.py`

| ID | EARS | Citation |
|---|---|---|
| **R-HE-001** | **U** `HungerEngine.tick(policy, clock, ledger, previous_phase, now)` shall return a `HungerSnapshot` with fields `drive_budget`, `work_pressure`, `active_hunger`, `drive_ratio`, `phase`, `should_stop`, `stop_reason`. | hunger_engine.py:32-97 |
| **R-HE-002** | **U** `drive_budget` shall be clamped to `[0, policy.h_max]`. | hunger_engine.py:54-55 |
| **R-HE-003** | **U** `active_hunger` shall be `min(drive_budget, work_pressure × h_max)`. | hunger_engine.py:57-58 |
| **R-HE-004** | **E** When `policy.decay_type == LOOP_COUNT` and `decay_duration_seconds <= 0`, `drive_budget` shall equal `policy.initial_hunger` (no decay). | hunger_engine.py:117-121 |
| **R-HE-005** | **E** When `policy.decay_type == LOOP_COUNT`, `drive_budget` shall be `initial_hunger × (max(0, max_loops − loop_count) / max_loops)`. At `loop_count == max_loops`, drive_budget is 0. | hunger_engine.py:117-121; test_loop_count_decay.py:53-72 |
| **R-HE-006** | **E** When `policy.decay_type == LINEAR` and `policy.started_at is None`, `drive_budget` shall equal `initial_hunger`. | hunger_engine.py:124-125 |
| **R-HE-007** | **E** When `policy.decay_type == LINEAR` and `started_at is not None`, `drive_budget` shall decay linearly from `initial_hunger` to 0 over `decay_duration_seconds` of wall-clock. | hunger_engine.py:127-130 |
| **R-HE-008** | **E** When `policy.decay_type == STAGE_BASED`, the engine shall compute drive_budget identically to LINEAR (MVP placeholder). | hunger_engine.py:132-134, 138-146 |
| **R-HE-009** | **E** When `clock.manually_cleared is True`, `drive_budget` shall be 0 regardless of decay type. | hunger_engine.py:112-113 |
| **R-HE-010** | **U** `tick` shall set `should_stop` and `stop_reason` according to this **strict priority order** (first match wins): | hunger_engine.py:65-87 |
| | 1. `clock.frozen` → `HUMAN_PAUSED` | test_loop_count_decay.py:109-115 |
| | 2. `consumed_by_cost_usd ≥ max_total_cost_usd` → `SAFETY_STOP` | test_loop_count_decay.py:118-126 |
| | 3. `consumed_tokens ≥ max_total_tokens` → `SAFETY_STOP` | hunger_engine.py:73-75 |
| | 4. `ledger.all_remaining_items_blocked()` → `BLOCKED` | test_loop_count_decay.py:84-92 |
| | 5. `drive_budget <= 0` and not done → `HUNGER_EXPIRED` | test_loop_count_decay.py:67-72 |
| | 6. `ledger.is_done()` → `DONE` | test_loop_count_decay.py:75-81 |
| **R-HE-011** | **U** `tick` shall set `phase` via hysteresis: `ratio > 0.6` → EXPLORE; `0.3 < ratio ≤ 0.6` → EXPLORE if `previous_phase == EXPLORE` else EXPLOIT; `ratio ≤ 0.3` → COOLDOWN. | hunger_engine.py:148-163 |
| **R-HE-012** | **E** When `policy.decay_type` is unknown, `tick` shall raise `NotImplementedError(f"{decay_type} not in MVP")`. | hunger_engine.py:136 |

### 3.3 Hunger update — `services/hunger_update.py`

| ID | EARS | Citation |
|---|---|---|
| **R-HU-001** | **E** When `report.verdict ∉ {PASS, PARTIAL}`, `apply_validation` shall **not** mutate any item and shall **not** call `repo.save_hunger_item`. | hunger_update.py:32-33; test_hunger_update.py:81-91 |
| **R-HU-002** | **U** `apply_validation` shall decrement each affected item's `gap_score` by `(count_of_newly_passed_keys_for_item / max(1, len(item.acceptance_checks)))`, clamped to `[0.0, gap_score]`. | hunger_update.py:35-49 |
| **R-HU-003** | **E** When item's id is in `report.satisfied_hunger_item_ids` AND `gap_score == 0.0`, the item's `status` shall be set to `VALIDATED_SATISFIED`. | hunger_update.py:52-58; test_hunger_update.py:64-78 |
| **R-HU-004** | **E** Otherwise, when newly_passed checks affected an item, the item's `status` shall be set to `WORKING`. | hunger_update.py:57-58 |
| **R-HU-005** | **U** `apply_validation` shall extend `item.evidence_ids` with `report.evidence_ids` and set `updated_at_loop = report.loop_id` for every affected item. | hunger_update.py:49-51 |
| **R-HU-006** | **K** (known issue) Float arithmetic in R-HU-002 may leave residue ≈1e-17 such that `gap_score == 0.0` is never reached even when all checks pass. PRD §28.4 (M-series, post-mining) addresses this. | hunger_update.py:48 |

### 3.4 Validation gate — `services/validation_gate.py`

| ID | EARS | Citation |
|---|---|---|
| **R-VG-001** | **U** `make_check_key(item_id, idx)` shall return `f"{item_id}:{idx}"`. | validation_gate.py:30-32; test_targeted_validation.py:15-17 |
| **R-VG-002** | **U** `validate(...)` shall fetch the baseline `BestState` via `repo.get_best_state(task_id)`; absence is treated as no previously-passed checks. | validation_gate.py:62-63 |
| **R-VG-003** | **U** `validate(...)` shall run a check iff (a) the check's hunger_item_id ∈ `target_hunger_item_ids`, OR (b) the check's `check_key` ∈ `previously_passed`. Untested checks remain in `currently_passed_check_keys` if they were previously passing. | validation_gate.py:77-118; test_targeted_validation.py:48-89 |
| **R-VG-004** | **U** A `CheckResult` shall have `previously_passed = (check_key in baseline.accepted_check_keys)`, `newly_passed = passed AND NOT previously_passed`, `regressed = previously_passed AND NOT passed`. | validation_gate.py:93-95 + test_check_level_progress.py:17-21 |
| **R-VG-005** | **E** When a candidate's `evidence_ids` is empty AND no shell-evidence is produced, `missing_evidence` shall contain `"Candidate produced no evidence."`. | validation_gate.py:128-130 |
| **R-VG-006** | **U** `has_real_progress` shall equal `len(newly_passed_check_keys) > 0`. | validation_gate.py:132 |
| **R-VG-007** | **U** Verdict shall be decided by this **strict priority order**: regressed → FAIL; missing_evidence → FAIL; (satisfied non-empty AND unsatisfied empty) → PASS; newly_passed non-empty → PARTIAL; else FAIL. | validation_gate.py:208-229 |
| **R-VG-008** | **U** Item satisfaction shall depend on `acceptance_mode`: `"all"` requires every result `passed`; `"any"` requires at least one. | validation_gate.py:188-200 |

### 3.5 Acceptance check runner — `services/acceptance_runner.py`

| ID | EARS | Citation |
|---|---|---|
| **R-AR-001** | **E** When `check_type == FILE_EXISTS`, `run` shall resolve `params["path"]` via `resolve_workspace_path(candidate_root, ...)` and return `(path.exists() and path.is_file(), detail, None)`. Evidence_id is `None`. | acceptance_runner.py:58-61 |
| **R-AR-002** | **E** When `check_type == SHELL_EXIT_ZERO`, `run` shall require `params["argv"]`, dispatch via `SandboxRunner.run_argv`, and return `(exit_code == 0 AND NOT timed_out, detail, evidence_id)`. | acceptance_runner.py:63-84 |
| **R-AR-003** | **E** When `SHELL_EXIT_ZERO` is missing `params["argv"]`, `run` shall raise `ValueError("SHELL_EXIT_ZERO requires params.argv in MVP.")`. | acceptance_runner.py:64-65 |
| **R-AR-004** | **E** When `check_type == EVIDENCE_COUNT_MIN`, `run` shall query `repo.count_evidence_by_type(...)` and return `(count >= min_count, detail, None)`. | acceptance_runner.py:86-95 |
| **R-AR-005** | **E** When `check_type == ARTIFACT_TYPE_EXISTS`, `run` shall return `(any(a.artifact_type == artifact_type), detail, None)`. | acceptance_runner.py:97-101 |
| **R-AR-006** | **E** When `check_type == HUMAN_APPROVAL`, `run` shall query `repo.is_approval_granted(approval_id)` and return that as `passed`. | acceptance_runner.py:103-106 |
| **R-AR-007** | **E** When `check_type == LLM_JUDGE`, `run` shall raise `NotImplementedError("LLM_JUDGE is V1.2+. Use binary checks in MVP.")`. | acceptance_runner.py:108-111 |
| **R-AR-008** | **E** When `check_type` is unknown, `run` shall raise `ValueError(f"Unknown check type: {ct}")`. | acceptance_runner.py:113 |
| **R-AR-009** | **U** Default `timeout` for SHELL_EXIT_ZERO is 60 seconds (overridable via `params["timeout"]`). | acceptance_runner.py:68 |

### 3.6 Commit manager — `services/commit_manager.py`

| ID | EARS | Citation |
|---|---|---|
| **R-CM-001** | **U** A candidate shall be promoted iff ALL of: (a) `verdict ∈ {PASS, PARTIAL}`, (b) `newly_passed_check_keys` non-empty, (c) `regressed_check_keys` empty, (d) `missing_evidence` empty. | commit_manager.py:89-99 |
| **R-CM-002** | **E** When promoted, the manager shall call `WorkspaceManager.promote_candidate_to_best(...)`, persist a `BestState` with `score=0.0`, `accepted_check_keys = report.currently_passed_check_keys`, `validation_id = report.id`, `workspace_ref = "best"`, and call `repo.mark_candidate_committed(candidate.id)`. | commit_manager.py:52-71; test_commit_manager.py:160-180 |
| **R-CM-003** | **E** When rejected, the manager shall call `WorkspaceManager.reject_candidate(...)`, `repo.mark_candidate_rejected(candidate.id)`, and `repo.add_failure_from_validation(report)`. It shall NOT call `repo.save_best_state`. | commit_manager.py:77-86; test_commit_manager.py:183-191 |
| **R-CM-004** | **U** Reject reason shall follow this priority: `verdict_fail` → `no_new_check_progress` → `regressed_checks_detected` → `missing_evidence` → `unknown`. | commit_manager.py:101-111; test_commit_manager.py:194-221 |
| **R-CM-005** | **U** `BestState.score` shall always be 0.0. Score-based commits are explicitly forbidden (I-3). | commit_manager.py:62 + test_commit_manager.py:179 |

### 3.7 Workspace manager — `services/workspace_manager.py`

| ID | EARS | Citation |
|---|---|---|
| **R-WM-001** | **U** Workspaces shall be laid out under `<root>/tasks/<task_id>/{best,candidates/loop_NNN,rejected/loop_NNN}/files/...`. Loop ids are zero-padded to 3 digits. | workspace_manager.py:35-45 |
| **R-WM-002** | **E** When `create_candidate_workspace(task, loop)` is called, the manager shall ensure `best/files/` exists, `rmtree` any pre-existing candidate of the same loop, then `copytree(best/files, candidate/files)`; if best is empty/absent, an empty candidate dir is created. | workspace_manager.py:51-73 |
| **R-WM-003** | **E** When `promote_candidate_to_best(task, loop)` is called and the candidate exists, the manager shall back up the current `best/` to `best_backup/`, `copytree(candidate, best)`, then remove the backup. The promotion is therefore atomic *only* if the move/copy/remove sequence does not crash mid-operation (see §6 U2). | workspace_manager.py:75-101 |
| **R-WM-004** | **E** When `promote_candidate_to_best` is called with a non-existent candidate, the manager shall raise `FileNotFoundError`. | workspace_manager.py:80-81; test_workspace_isolation.py:73-76 |
| **R-WM-005** | **E** When `reject_candidate(task, loop)` is called, the manager shall move the candidate directory to `rejected/loop_NNN/files/`, `rmtree`-ing any pre-existing rejected dir of the same loop. If the candidate does not exist, the call is a no-op. | workspace_manager.py:103-123 |
| **R-WM-006** | **U** Every workspace operation shall write a `manifest.json` in the parent directory containing `task_id`, `loop_id`, `path`, `status`, `created_at` (UTC ISO-8601), `file_count`, `total_bytes`, `source_workspace_ref`. | workspace_manager.py:125-147; test_workspace_isolation.py:79-82 |
| **R-WM-007** | **S** While `best/` exists, candidate writes shall not affect it. (Tested by writing to candidate and asserting `best` is unchanged.) | test_workspace_isolation.py:19-29 |

### 3.8 Sandbox runner — `services/sandbox_runner.py`

| ID | EARS | Citation |
|---|---|---|
| **R-SB-001** | **E** When `argv` is empty, `run_argv` shall raise `ValueError("argv cannot be empty")` before spawning. | sandbox_runner.py:77-78; test_sandbox_runner.py:87-96 |
| **R-SB-002** | **E** When `timeout <= 0`, `run_argv` shall raise `ValueError("timeout must be positive")`. | sandbox_runner.py:80-81; test_sandbox_runner.py:99-108 |
| **R-SB-003** | **E** When `cwd` does not exist or is not a directory, `run_argv` shall raise `ValueError(f"cwd does not exist or is not a directory: {cwd}")`. | sandbox_runner.py:83-84; test_sandbox_runner.py:151-161 |
| **R-SB-004** | **U** On non-Windows platforms, the subprocess shall be spawned with `start_new_session=True` so the runner can SIGKILL the entire process group on timeout. | sandbox_runner.py:86-92, 100-104 |
| **R-SB-005** | **E** When the subprocess exceeds `timeout`, `run_argv` shall SIGKILL the process group (or `proc.kill()` on Windows), await `communicate()` to drain output, and set `timed_out=True`. | sandbox_runner.py:99-108 |
| **R-SB-006** | **U** Stdout and stderr shall be UTF-8 decoded with `errors="replace"`, then sliced to `max_output_chars` (default 5000). Decoding before slicing is intentional to avoid splitting multi-byte sequences. | sandbox_runner.py:110-113 |
| **R-SB-007** | **U** `run_argv` shall always invoke `repo.save_shell_output_as_evidence(...)` after `communicate()` returns, and propagate any exception from that call (no silent swallow). | sandbox_runner.py:118-131 |
| **R-SB-008** | **U** A non-zero exit shall NOT blank stdout — partial output must be preserved for the agent to debug. | sandbox_runner.py:110-116; test_sandbox_runner.py:137-148 |
| **R-SB-009** | **U** `proc.returncode` is documented as guaranteed non-None after `communicate()`; the implementation falls back to -1 defensively. | sandbox_runner.py:114-116 |

### 3.9 Cost guard — `services/cost_guard.py`

| ID | EARS | Citation |
|---|---|---|
| **R-CG-001** | **E** When `clock.consumed_by_cost_usd >= policy.max_total_cost_usd`, `assert_within_budget` shall raise `SafetyStopError("Cost ceiling hit: ...")`. | cost_guard.py:38-42 |
| **R-CG-002** | **E** When `clock.consumed_tokens >= policy.max_total_tokens`, `assert_within_budget` shall raise `SafetyStopError("Token ceiling hit: ...")`. | cost_guard.py:44-48 |
| **R-CG-003** | **U** `record_llm_usage(task_id, usage)` shall add `usage.input_tokens + usage.output_tokens` to `consumed_tokens`, add `usage.cost_usd` to `consumed_by_cost_usd`, persist via `repo.save_hunger_clock`, and then call `assert_within_budget`. | cost_guard.py:50-64; test_cost_guard.py:52-66 |
| **R-CG-004** | **U** `record_tool_cost(task_id, cost_usd, tokens)` shall update the same clock fields and call `assert_within_budget`. | cost_guard.py:66-83; test_cost_guard.py:69-74 |
| **R-CG-005** | **U** Both record methods may raise `SafetyStopError` *after* the update is persisted — that is, the clock is "over budget" at the moment of the raise. (Acceptable: the record is durable; the next `assert_within_budget` call from any caller still trips.) | cost_guard.py:60-64 |

### 3.10 Stagnation detector — `services/stagnation_detector.py`

| ID | EARS | Citation |
|---|---|---|
| **R-SD-001** | **U** Only items in `validation_report.attempted_hunger_item_ids` shall be tracked; non-attempted items are ignored. (Encodes I-6.) | stagnation_detector.py:53, 62 |
| **R-SD-002** | **E** When an attempted item has any newly_passed check (item in `newly_progressed`), its `consecutive_failure_count` shall be reset to 0 and `last_progress_loop_id` updated. | stagnation_detector.py:67-69 |
| **R-SD-003** | **E** Otherwise, an attempted item's `consecutive_failure_count` shall be incremented by 1. | stagnation_detector.py:70-71 |
| **R-SD-004** | **E** When `consecutive_failure_count >= max_item_consecutive_failures` (default 3), the item's `status` shall be set to `BLOCKED` and the item id added to `blocked_items`. | stagnation_detector.py:73-77 |
| **R-SD-005** | **E** When `validation_report.has_real_progress` is True, the global no-progress streak shall be reset; otherwise it shall be incremented. | stagnation_detector.py:79-84 |
| **R-SD-006** | **E** When the global streak `>= max_global_no_progress_loops` (default 5), `update` shall return `global_blocked = True`. | stagnation_detector.py:84-87 |

### 3.11 Requirement compiler — `services/requirement_compiler.py`

| ID | EARS | Citation |
|---|---|---|
| **R-RC-001** | **E** When `hints["core_acceptance_checks"]` is missing or non-list, `compile` shall raise `ValueError("MVP requires core_acceptance_checks.")`. | requirement_compiler.py:52-54 |
| **R-RC-002** | **U** `compile` shall always emit `H-001` (Core deliverable, priority 1.0, GOAL_GAP) using the user-supplied checks and `acceptance_mode`. | requirement_compiler.py:61-73 |
| **R-RC-003** | **U** `compile` shall always emit `H-002` (Sufficient evidence, priority 0.7, EVIDENCE_COUNT_MIN ≥ 1, mode "all"). | requirement_compiler.py:75-91 |
| **R-RC-004** | **O** When `hints["enable_memory_consolidation"]` is True, `compile` shall additionally emit `H-003` (Memory consolidation, priority 0.4, MEMORY_CONSOLIDATION type, HUMAN_APPROVAL check, approval_id `f"{task_id}-memory"`). | requirement_compiler.py:93-111 |
| **R-RC-005** | **E** When `hints["core_acceptance_mode"]` is neither `"all"` nor `"any"`, the compiler shall fall back to `"all"`. | requirement_compiler.py:56-59 |

### 3.12 Path safety — `services/path_safety.py`

| ID | EARS | Citation |
|---|---|---|
| **R-PS-001** | **E** When `user_path` is empty or whitespace-only, `resolve_workspace_path` shall raise `ValueError("Empty or whitespace-only path is not allowed.")`. | path_safety.py:38-39 |
| **R-PS-002** | **E** When `user_path` is absolute, the function shall raise `PermissionError(f"Absolute path is not allowed: {user_path}")`. | path_safety.py:42-43 |
| **R-PS-003** | **E** When the resolved path is not relative to `workspace_root.resolve()`, the function shall raise `PermissionError(f"Path escapes workspace: {user_path}")`. | path_safety.py:47-48 |
| **R-PS-004** | **U** The function shall return `Path` objects via `(root / raw).resolve()` — symlink resolution applies, so a symlink inside the root pointing outside is rejected. | path_safety.py:45-48 |

### 3.13 Integrator — `services/integrator.py`

| ID | EARS | Citation |
|---|---|---|
| **R-IN-001** | **U** `integrate(task_id, loop_id, results)` shall return a `CandidateState` whose `summary` is `"\n".join(non-empty result.summary)` in input order. | integrator.py:14-43 |
| **R-IN-002** | **U** `artifact_ids`, `evidence_ids`, `claim_ids` shall be deduplicated *order-preserving* via `dict.fromkeys`. | integrator.py:44-46 |
| **R-IN-003** | **U** `proposed_score` shall always be 0.0. | integrator.py:47 |
| **R-IN-004** | **U** `id` shall be `f"CAND-{task_id}-{loop_id}"` and `workspace_ref` shall be `f"candidates/loop_{loop_id:03d}"`. | integrator.py:40, 48 |

### 3.14 Context builder — `services/context_builder.py`

| ID | EARS | Citation |
|---|---|---|
| **R-CB-001** | **U** `build_for_agent` shall populate `acceptance_criteria` with `description` strings of every check across the targeted hunger items, in item order then check order. | context_builder.py:50-54 |
| **R-CB-002** | **U** `best_state_summary` shall be `repo.get_best_state(task_id).summary` if a best exists, else `None`. | context_builder.py:48, 64 |
| **R-CB-003** | **U** The `phase` field shall be the `BudgetAllocation.phase.value` (string). | context_builder.py:61 |
| **R-CB-004** | **U** `budget` shall be a dict `{"max_tokens": ..., "max_tool_calls": ...}` (NOT the BudgetAllocation instance). | context_builder.py:67-70 |

### 3.15 CLI

| ID | EARS | Citation |
|---|---|---|
| **R-CL-001** | **U** `hungerloop --version` shall print `0.4.1`. | cli/main.py:15 |
| **R-CL-002** | **U** `hungerloop workspace best <task_id>` shall print every regular file under `<root>/tasks/<task_id>/best/files/`, sorted, with size in bytes. Default `--root` is `workspace`. | cli/workspace_cmd.py:14-26 |
| **R-CL-003** | **U** `hungerloop workspace candidate <task_id> --loop N` shall print every file under `<root>/tasks/<task_id>/candidates/loop_NNN/files/`. | cli/workspace_cmd.py:29-44 |
| **R-CL-004** | **U** `hungerloop workspace rejected <task_id> --loop N` shall print every file under `<root>/tasks/<task_id>/rejected/loop_NNN/files/`. | cli/workspace_cmd.py:47-62 |
| **R-CL-005** | **E** When the queried directory does not exist, `workspace` commands shall print a single line ("No best workspace for task X" / similar) and exit 0. | cli/workspace_cmd.py:20-22 etc. |
| **R-CL-006** | **U** `hungerloop checks <task_id>` shall connect to `<workspace>/tasks/<task_id>/blackboard.sqlite` (or `--db <path>`) and print `<check_key>  PASS  loop=<N>  <validation_id>` for each row in `accepted_checks` for the task. | cli/checks_cmd.py:10-43 |
| **R-CL-007** | **E** When the SQLite file does not exist, `checks` shall print `"No database found at <path>"` and exit 0. | cli/checks_cmd.py:20-22 |
| **R-CL-008** | **E** When the `accepted_checks` table does not exist, `checks` shall print `"accepted_checks table not found"` and exit 0. | cli/checks_cmd.py:31-33 |

### 3.16 Repository protocol — `repository/protocol.py`

| ID | EARS | Citation |
|---|---|---|
| **R-RP-001** | **U** The Protocol shall declare 22 methods covering hunger state, candidate/best states, validation reports, evidence, approvals, no-progress streak, loop ids, and event append. | protocol.py:17-67 |
| **R-RP-002** | **U** `next_loop_id(task_id)` shall return strictly increasing integers starting at 1. | in_memory_repo.py:159-162 |
| **R-RP-003** | **U** `count_evidence_by_type(task_id, evidence_ids, evidence_type)` shall count entries in `evidence_ids` whose stored type matches; `evidence_type == "any"` returns `len(evidence_ids)` without filtering. | in_memory_repo.py:107-117 |
| **R-RP-004** | **U** `is_approval_granted(approval_id)` shall be True iff the id has been added via implementation-specific `grant_approval`. | in_memory_repo.py:122-126 |
| **R-RP-005** | **U** `save_best_state` overwrites by `task_id`; only one BestState exists per task at a time. | in_memory_repo.py:41-42 |
| **R-RP-006** | **K** (implicit contract) `save_hunger_clock` is a no-op in `InMemoryRepository`; mutations on the dict-cached instance returned by `get_hunger_clock` are implicitly persisted via reference. SQLite cannot satisfy this contract — see §6 U3. | in_memory_repo.py:65-71 |

---

## 4. Non-functional Observations

| NFR | Observation | Citation |
|---|---|---|
| **Type safety** | `mypy --strict` passes with `pydantic.mypy` plugin disabled. 6 services hold `repo: Any` with `TODO(Task 14)` markers; static type-checking does not see `RepositoryProtocol`. | pyproject.toml:30-36, multiple services |
| **Test coverage** | 89 unit tests across 13 files, 1.5k LOC of tests for ~2.1k LOC of source. No integration tests despite `tests/integration/__init__.py` existing. | tests/unit/ |
| **Async surface** | Async limited to 4 callsites: `SandboxRunner.run_argv`, `AcceptanceCheckRunner.run`, `ValidationGate.validate`, and `pytest-asyncio auto` mode. Everything else is sync. | grep `async def` |
| **External I/O** | Filesystem only (subprocess + `pathlib`). No network, no DB connections (despite `sqlite3` import in `cli/checks_cmd.py`). No env-var reads anywhere in `src/`. | grep `os.environ\|os.getenv\|config\[` empty |
| **Process isolation** | Subprocess runs in new process group via `start_new_session=True` (POSIX). Timeout uses `os.killpg(SIGKILL)` then `proc.communicate()`. | sandbox_runner.py:86-108 |
| **Path safety** | All user paths in acceptance checks routed through `resolve_workspace_path` (rejecting absolute, escaping, empty). Workspace promotion uses `Path.resolve()` + `is_relative_to`. | path_safety.py + acceptance_runner.py:59 |
| **Cost containment** | Two-tier ceiling on `consumed_by_cost_usd` and `consumed_tokens` from `HungerPolicy`. Pre-call assertion + post-call record-and-assert. Mutating the clock object directly is required for in-memory persistence to work (see U3). | cost_guard.py |
| **Concurrency** | No locks. `InMemoryRepository` is not thread-safe (all dict mutations unprotected). Single-orchestrator-per-task is the implicit assumption. | in_memory_repo.py |
| **Determinism** | `make_check_key` is pure. `Integrator` dedup is order-preserving. `RuleBasedCompiler` produces identical ledgers for identical input. `SandboxRunner` evidence_id is RNG-seeded by repository implementation — `InMemoryRepository` uses `uuid.uuid4()` (non-deterministic). | in_memory_repo.py:140 |
| **Frozen models** | `BestState`, `CandidateState`, `CheckResult`, `ValidationReport` use `ConfigDict(frozen=True)`. `HungerItem`, `HungerLedger`, `HungerClockState`, `HungerPolicy`, `HungerSnapshot` are mutable. Mutability of `HungerItem` is exploited by `HungerUpdateService` and `StagnationDetector` (in-place updates). | blackboard.py / validation.py vs hunger.py |
| **Logging** | No structured logging in `src/`. The `events` list in `InMemoryRepository` is the only event sink, populated only by `append_event`. No callers exist in `src/` (only available via `repo.append_event(...)`). | grep `append_event` in src/ → only the impl |
| **Error taxonomy** | Custom: `SafetyStopError(RuntimeError)`. Stdlib: `ValueError`, `PermissionError`, `FileNotFoundError`, `NotImplementedError`. No domain hierarchy. | grep `class.*Error` |

---

## 5. Inferred Acceptance Criteria

These are reverse-engineered from test names + test bodies; treat as the v0.4.1 contract that v0.5a refactors must preserve.

```text
HungerEngine
  - tick at loop_count=0 with LOOP_COUNT decay returns drive_budget == initial_hunger.
  - tick at loop_count==max_loops returns drive_budget == 0 AND should_stop AND HUNGER_EXPIRED.
  - tick at loop_count == max_loops - 1 still allows the loop (drive_budget > 0).
  - All-DONE ledger triggers DONE.
  - All-BLOCKED ledger triggers BLOCKED, even when drive_budget > 0.
  - frozen clock triggers HUMAN_PAUSED first.
  - Cost ceiling triggers SAFETY_STOP before BLOCKED check.

HungerLedger
  - Empty ledger: is_done() == True, all_remaining_items_blocked() == False.
  - Mixed BLOCKED + OPEN: all_remaining_items_blocked() == False, has_active_items() == True.
  - All-BLOCKED, gap > 0: all_remaining_items_blocked() == True, is_done() == False.
  - All-CLOSED + VALIDATED_SATISFIED, gap == 0: is_done() == True.

ValidationGate
  - Only target items have all checks evaluated.
  - Untargeted items contribute zero CheckResults.
  - Previously-passed checks of untargeted items are run as regression checks.
  - A previously-passed check that fails now appears in regressed_check_keys.
  - regressed → verdict=FAIL.

CommitManager
  - PASS or PARTIAL with newly_passed checks AND no regressions AND evidence → commit.
  - Empty newly_passed → reject(no_new_check_progress).
  - Any regressed_check_keys → reject(regressed_checks_detected).
  - missing_evidence → reject(missing_evidence).
  - FAIL verdict → reject(verdict_fail), highest priority among reject reasons.
  - Commit promotes the candidate workspace and persists BestState with score==0.0.
  - Reject moves candidate to rejected/ and never writes BestState.

WorkspaceManager
  - Candidate creation copies best/ into candidates/loop_NNN/.
  - Candidate writes never affect best/ before promotion.
  - Promotion replaces best/ with candidate's contents.
  - Reject moves candidate to rejected/loop_NNN/, leaves best/ unchanged.
  - manifest.json written on every state transition.
  - Promote on missing candidate raises FileNotFoundError.

SandboxRunner
  - Empty argv → ValueError before spawn.
  - timeout <= 0 → ValueError.
  - Missing cwd → ValueError.
  - Subprocess timeout → SIGKILL process group, timed_out=True.
  - Output truncated to max_output_chars.
  - Decode-then-truncate (multi-byte safe).
  - Non-zero exit preserves stdout.
  - Always saves shell evidence; raises propagate.

CostGuard
  - cost == ceiling triggers raise (>=).
  - tokens == ceiling triggers raise (>=).
  - record_llm_usage updates clock then asserts.
  - record_tool_cost updates clock then asserts.

HungerUpdateService
  - FAIL verdict: no item mutations.
  - PARTIAL with 1/2 checks passed: gap_score 1.0 → 0.5.
  - PASS with last check + satisfied: gap_score → 0.0, status → VALIDATED_SATISFIED.

StagnationDetector
  - Attempted item with progress: failure_count = 0.
  - Attempted item without progress: failure_count++.
  - failure_count >= 3 → status BLOCKED.
  - has_real_progress: global streak resets.
  - global streak >= 5: global_blocked True.

RuleBasedCompiler
  - Missing core_acceptance_checks → ValueError.
  - Default produces H-001 (user checks) + H-002 (evidence ≥ 1).
  - enable_memory_consolidation=True adds H-003 (HUMAN_APPROVAL).

path_safety
  - Empty/whitespace path → ValueError.
  - Absolute path → PermissionError.
  - Escape attempt → PermissionError.
```

---

## 6. Uncertainties & Discovered Issues

These are facts I observed in the code that warrant explicit clarification, separate from anything covered by the v0.5.2 PRD §28 patches.

### U1. **`hungerloop checks` references SQLite that v0.4.1 doesn't ship**
- `cli/checks_cmd.py:16` constructs path `workspace/tasks/<task_id>/blackboard.sqlite` and queries `accepted_checks` table.
- `InMemoryRepository` has no SQLite backend; `SQLiteRepository` does not exist.
- **Result**: this command always prints `"No database found at ..."` for every real task. It's a half-implemented feature.
- **Recommendation**: either (a) remove the command from v0.5a until SQLiteRepository ships, or (b) document it as a forward-compatibility stub in `CLAUDE.md`.

### U2. **`promote_candidate_to_best` is not actually atomic**
- workspace_manager.py:75-101 sequence: `best → best_backup` (move), `candidate → best` (copytree), `rmtree(best_backup)`. A crash between move and copytree leaves no `best/` directory at all; a crash mid-copytree leaves a partial best.
- The docstring says "atomically replace" but the code does not use `os.rename` (which would be atomic on POSIX for same-filesystem moves) for the candidate→best step.
- **Recommendation**: either rename the docstring to "back-up-then-replace" or refactor to use `os.replace` after staging (requires putting candidate alongside best on the same filesystem, which it already is).

### U3. **`save_hunger_clock` is a no-op in `InMemoryRepository`**
- in_memory_repo.py:65-71: `get_hunger_clock` returns the cached instance; `save_hunger_clock` is `pass`. Callers mutate the returned object, which is implicitly persisted because the repo holds the same reference.
- This is a hidden contract: any new repository implementation (SQLite, Postgres) MUST also return a reference that callers can mutate, OR the semantic must change to "callers always pass a copy to save".
- `CostGuard` relies on this: `clock = repo.get_hunger_clock(task_id); clock.consumed_tokens += ...; repo.save_hunger_clock(clock)` — for `InMemoryRepository`, the `save_hunger_clock` call is decorative.
- **Recommendation**: change semantics to copy-on-save (the SQLiteRepository will require this). Update CostGuard's docstring to note that `save_hunger_clock` is mandatory.

### U4. **`StopReason.HUMAN_REQUIRED` and `StopReason.ERROR` already exist in v0.4.1**
- enums.py:8, 11 — both values are present.
- Earlier review rounds (v0.5.1 / v0.5.2 PRD §3.1) claimed these were missing in v0.4.1 and treated their addition as a schema change. This was a misread of the source.
- **Recommendation**: edit PRD §3.1 to remove the "NEW in v0.5.2" markers; HUMAN_REQUIRED and ERROR are pre-existing. The `WorkerResult.requires_human` field IS new (v0.4.1 has no such field) — that part of the PRD is correct.

### U5. **`StopReason.BLOCKED` priority above `HUNGER_EXPIRED` causes counter-intuitive behavior**
- hunger_engine.py:77-79 — the engine returns BLOCKED before checking `drive_budget <= 0`. If all items are BLOCKED AND drive_budget is also 0, the user sees BLOCKED, not HUNGER_EXPIRED, even though both apply.
- The CLAUDE.md priority order documents BLOCKED above HUNGER_EXPIRED, so this is intentional. But `--resume` flow in PRD §18.3 differentiates the two stop reasons (BLOCKED requires `unblock`, HUNGER_EXPIRED requires `refill`); a task with both conditions will require `unblock` first, then `refill`.
- **Recommendation**: document this in PRD §15 / §18.3.

### U6. **`InMemoryRepository` exposes 3 methods not in `RepositoryProtocol`**
- `set_hunger_policy(task_id, policy)`, `set_hunger_ledger(task_id, ledger)`, `grant_approval(approval_id)` are public on `InMemoryRepository` (in_memory_repo.py:62-63, 78-81, 125-126) but absent from `protocol.py`.
- They are used by tests as setup helpers (test_loop_count_decay.py uses neither, but other tests likely do via fixtures).
- **Effect**: `RepositoryProtocol` is incomplete relative to what's actually needed for test ergonomics. SQLiteRepository will need equivalent setup paths or migrations.
- **Recommendation**: either (a) add `setup_*` test-only methods to a separate `TestRepositorySupport` Protocol, or (b) accept that these are bootstrap helpers and document them.

### U7. **`Integrator.proposed_score` is hardcoded to 0.0 — confirms I-3 in code, but field is dead weight**
- integrator.py:47 — `proposed_score=0.0`. `BestState.score=0.0` (commit_manager.py:62).
- The field exists "for schema only" (per the comment) but no code reads it.
- **Recommendation**: keep for backward compat, or remove with an explicit migration note. Removing is safer if any future reader assumes scores are meaningful.

### U8. **`ContextBuilder.budget` is a `dict[str, object]`, not a `BudgetAllocation`**
- context_builder.py:67-70 constructs the dict; ContextPack.context.py:33 declares `budget: dict[str, object]`.
- This is the "M3" issue from PRD review, already addressed in PRD §28.7. Documenting here for completeness: the v0.4.1 source DOES have this dict-typed budget, and ContextBuilder is the producer.

### U9. **`save_hunger_ledger` does not exist on `RepositoryProtocol`**
- protocol.py has `get_hunger_ledger` but no save. `set_hunger_ledger` lives only on InMemoryRepository.
- Mutations to a ledger (e.g., adding items) cannot be persisted via the protocol surface today.
- v0.4.1 has no callers of "ledger mutation"; the ledger is effectively read-only after initial compilation. v0.5a will need this method when refill / replenishment lands.

### U10. **`LoopTrace.candidate_state_id` and `validation_report_id` are `str` (not `str | None`)**
- tracing.py:23-24 — both required. But Orchestrator pseudocode in PRD §28 has paths (empty plan, SafetyStopError) where no candidate or validation exists. Building a LoopTrace in those paths will require placeholder strings or a model change.
- **Recommendation**: change to `str | None` in v0.5a, or document the placeholder convention (e.g. `"none"`).

---

## 7. Recommendations

### 7.1 v0.5a "must-do-without-breaking" list (regression risk)

These behaviors have explicit tests; v0.5a refactors must preserve them:

- StopReason priority order (R-HE-010) — tested in `test_loop_count_decay.py:67-126`.
- Reject reason priority (R-CM-004) — tested in `test_commit_manager.py:194-221`.
- Verdict decision tree (R-VG-007) — tested in `test_targeted_validation.py` and `test_check_level_progress.py`.
- Path safety rejection rules (R-PS-001..003) — tested in `test_path_safety.py`.
- `make_check_key` format `"{item_id}:{idx}"` (R-VG-001) — universally assumed.
- `dict.fromkeys` order-preserving dedup in Integrator (R-IN-002) — implicit but observable.
- `resolve_workspace_path` calls `Path.resolve()` (symlink follow) — security-relevant.

### 7.2 v0.5a "fix-while-touching" list

When v0.5a edits these files, address the latent issues:

- **U2** (atomicity) — fix promotion to use `os.replace` or document the non-atomicity.
- **U3** (save_hunger_clock no-op) — change to copy-on-save before SQLiteRepository lands.
- **U4** (StopReason already present) — edit PRD §3.1 to remove the "NEW" markers.
- **U6** (Repository setup helpers) — formalize test-support protocol or add to RepositoryProtocol.
- **U10** (LoopTrace required str fields) — make optional before Orchestrator wires up.

### 7.3 v0.5a "should-not-break" CLI

- `hungerloop --version` (R-CL-001).
- `hungerloop workspace best/candidate/rejected <task_id>` (R-CL-002..004).
- `hungerloop checks` (R-CL-006) — currently prints "No database found"; will start working once SQLiteRepository ships. Acceptance test: invoke against a freshly-created task and expect "No accepted checks." instead of "No database found at...".

### 7.4 Spec maintenance

- Re-mine after v0.5a Day 7 (post Orchestrator + ToolHarness wiring) — significant new behavior surface.
- Add integration tests under `tests/integration/` (currently empty); the unit tests cover contracts but not end-to-end loop semantics.
- Convert this spec into per-module `docs/spec/` files when v0.5a code lands, so each spec lives next to its module.

---

## 8. Quick-Reference Symbol Index

| Symbol | File | Line |
|---|---|---|
| `StopReason` enum | `models/enums.py` | 4 |
| `ValidationVerdict` enum | `models/enums.py` | 14 |
| `HungerEngine.tick` | `services/hunger_engine.py` | 32 |
| `HungerLedger.is_done` | `models/hunger.py` | 122 |
| `HungerLedger.all_remaining_items_blocked` | `models/hunger.py` | 116 |
| `ValidationGate.validate` | `services/validation_gate.py` | 43 |
| `make_check_key` | `services/validation_gate.py` | 30 |
| `CommitManager.apply` | `services/commit_manager.py` | 35 |
| `CommitManager._can_commit` | `services/commit_manager.py` | 89 |
| `WorkspaceManager.create_candidate_workspace` | `services/workspace_manager.py` | 51 |
| `WorkspaceManager.promote_candidate_to_best` | `services/workspace_manager.py` | 75 |
| `WorkspaceManager.reject_candidate` | `services/workspace_manager.py` | 103 |
| `SandboxRunner.run_argv` | `services/sandbox_runner.py` | 47 |
| `CostGuard.assert_within_budget` | `services/cost_guard.py` | 26 |
| `CostGuard.record_llm_usage` | `services/cost_guard.py` | 50 |
| `StagnationDetector.update` | `services/stagnation_detector.py` | 37 |
| `RuleBasedCompiler.compile` | `services/requirement_compiler.py` | 27 |
| `resolve_workspace_path` | `services/path_safety.py` | 11 |
| `Integrator.integrate` | `services/integrator.py` | 14 |
| `ContextBuilder.build_for_agent` | `services/context_builder.py` | 20 |
| `RepositoryProtocol` | `repository/protocol.py` | 17 |
| `InMemoryRepository` | `repository/in_memory_repo.py` | 18 |
| `cli` (click group) | `cli/main.py` | 14 |
| `checks` (click cmd) | `cli/checks_cmd.py` | 10 |
