# HungerLoop v0.7.0

**English** | [简体中文](README.zh-CN.md)

A Python async agent iterative-loop framework built on "check-level commits" and a "hunger budget". v0.7 Loop-Objective Evolution builds on the v0.6 mission runtime with spec-to-check synthesis, worker discovery credit, ADR-010 refactor transactions, Layer-3 memory auto-promotion and cross-task recall, while keeping the mission CLI and the one-way SQLite→artifact mirror.

> **Status**: v0.7.0: Loop-Objective Evolution GA. The final gate verified all 102 assertions passing: `pytest` 1747 passed / 1 skipped / 20 approved deselected, `mypy --strict src/` 104 files clean, `ruff check src/ tests/` clean, CLI smoke passed, no persistent services/ports.
>
> **Branch progress (`v0.7.1-v0.7.2`, pushed)**: cold-start draft sampling (`--draft-k` / `draft_sampling_k`), policy-configurable global no-progress fuse (`max_global_no_progress_loops`), ADR-010 refactor transaction auto-open, `TOOL_CALL_FAILED` error attribution, seed-hint regression naming.

---

## Table of Contents

1. [Core Concepts](#core-concepts)
2. [Feature Matrix](#feature-matrix)
3. [Installation](#installation)
4. [v0.7 Mission Quickstart](#v07-mission-quickstart)
5. [5-Minute Quickstart (legacy acceptance checks)](#5-minute-quickstart-legacy-acceptance-checks)
6. [Full Workflow](#full-workflow)
7. [CLI Guide](#cli-guide)
8. [Acceptance Spec File](#acceptance-spec-file)
9. [Model Configuration](#model-configuration)
10. [Invariants Reference](#invariants-reference)
11. [Project Structure](#project-structure)
12. [Development & Testing](#development--testing)
13. [Docs & Roadmap](#docs--roadmap)

---

## Core Concepts

HungerLoop is a runtime that "compiles" long agent tasks into an observable, interruptible, resumable iterative loop. Instead of asking the model to finish the task in one shot, it decomposes the task into a set of **acceptance checks**; each loop attempts to pass one or more of them, constrained by the following principles:

- **Hunger**: every task has a total cost/token/loop budget called its "hunger value". Every unit of resource consumed makes the task hungrier; when the budget is exhausted the task ends in `HUNGER_EXPIRED`. You can feed it with `hungerloop hunger refill` to keep it running.
- **Check-level commits (I-3)**: a candidate state is committed into `best/` only when it **actually turns a previously failing check green** and **causes no regression**. Commits are never score-based.
- **Workspace isolation (I-4)**: the model can only read `best/` and writes to `candidates/loop_NNN/`. Only the `CommitManager` can promote a candidate to best.
- **Cost guard (I-8)**: budgets are checked before and after every LLM/tool call; any overrun triggers an immediate `SAFETY_STOP`.
- **BLOCKED ≠ DONE (I-9)**: all hunger items blocked by a human is not task completion. Stop reasons have a strict priority: `HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`.

See [Invariants Reference](#invariants-reference) for the full list.

---

## Feature Matrix

| Module | Status | Notes |
| ---- | ---- | ---- |
| `LoopOrchestrator` | ✅ | Full hunger → plan → execute → validate → commit loop (PRD §12) |
| `RuleBasedPlanner` | ✅ | Rule-based planner driven by `priority × gap_score` (§5) |
| `WorkerRuntime` + `ExecutionWorker` | ✅ | `BudgetGuard`, side-effect gating, `ToolNotPermitted` (§6/§7/§28.11) |
| `DummyModelClient` / `OpenAIModelClient` | ✅ | Retries, JSON safety, `Retry-After`, error evidence persisted (§11.4 / §28.2 / §28.3) |
| `ModelConfig` + `PricingTable` | ✅ | YAML config, no plaintext keys, environment variables only (§10 / §11.3) |
| `MemoryManager` | ✅ | Generates `MemoryCandidate` each loop with deterministic predicates; v0.7 adds Layer-3 auto-promotion and cross-task recall (§19) |
| `SkillManager` | ✅ | Grants a skill card when `DONE` with ≥2 passing checks (§20) |
| `SQLiteRepository` | ✅ | Forward migrations, WAL, usage_snapshots, task_locks, events, traces, reports, memory, skill |
| `MissionRuntime` | ✅ | `MissionPlanner`, `WorkerScheduler`, `HandoffProcessor`, `ValidationPipeline`, `MissionStateUpdater`, 7 mission CLI subcommands (v0.6 runtime, extended in v0.7) |
| `SpecCheckSynthesizer` | ✅ | Synthesizes validation checks from mission/spec coverage gaps (v0.7) |
| Worker discovery credit | ✅ | Structured handoffs can propose checks; discovery work lands in the ledger via the compiler path (v0.7) |
| Refactor transactions (ADR-010) | ✅ | Declarative, time-boxed, policy-gated I-3 regression waivers limited to transaction-declared check keys (v0.7); branch adds policy-gated auto-open (`refactor_auto_open_enabled` + `refactor_transactions_enabled`, off by default, with min-newly floor and 3x net-positive ratio gate) |
| Cross-task memory recall | ✅ | Promoted Layer-3 memories can be recalled by relevance in new tasks (v0.7) |
| Draft sampling (cold start) | ✅ | `--draft-k` / `policy.draft_sampling_k` (1..5, default 1=off): the first loop drafts k candidates, picks a winner score-free, archives loser drafts, persists only the winner's handoffs, and records a `DRAFT_SAMPLED` event (v0.7.1-v0.7.2 branch) |
| Policy-configurable global fuse | ✅ | `max_global_no_progress_loops` (default 5) is policy-configurable; rejected candidates always increment the no-progress streak even when their raw report contains newly-passed checks (no momentum hold), and the fuse emits a `GLOBAL_STAGNATION_BLOCKED` event (v0.7.1-v0.7.2 branch) |
| `LearningWorker` / `ResearchWorker` | ⏳ | v0.5d |
| `LLMPlanner` + true concurrent fan-out/join | ⏳ | Future release |
| `Azure OpenAI` runtime | ⏳ | Placeholder implementation; fails explicitly when called |
| Long-term memory production promotion flow | ✅ | v0.7 Layer-3 promotion gate, auto-promotion and cross-task recall |

---

## Installation

Requires Python 3.11+. A virtual environment is recommended for local development; on Windows PowerShell use `.venv\Scripts\Activate.ps1`, on POSIX shells use `source .venv/bin/activate`.

Environment conventions:

- The `dummy` provider needs no API key and is ideal for deterministic local regression.
- The OpenAI runtime only reads environment variable names such as `OPENAI_API_KEY`; never write plaintext secrets in YAML, README, or `.env` contents.
- `HUNGERLOOP_LOCK_STALE_SEC` overrides the stale-lock threshold; `HUNGERLOOP_MISSION_RUNTIME=0` is only a legacy v0.6 rollback flag — the normal v0.7 path does not depend on it.

```bash
# Clone and install (including the dev toolchain)
git clone <repo>
cd hungerloop
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Verify (v0.7 final gate)
hungerloop --version
pytest tests/        # final gate: 1747 passed, 1 skipped, 20 approved Windows baseline deselected
mypy --strict src/   # 104 files clean
ruff check src/ tests/
```

---

## v0.7 Mission Quickstart

v0.7 keeps using the mission artifacts and CLI entry points introduced in v0.6, and adds spec-to-check synthesis, worker discovery credit, refactor transactions, and memory recall at runtime. Create a minimal mission spec, then create, run, and inspect it through the mission runtime:

```bash
# 1. Prepare the mission artifacts
mkdir -p demo-mission
cat > demo-mission/mission.md <<'MD'
# Demo Mission

## Description
Generate a short report through the v0.7 mission runtime.
MD

cat > demo-mission/features.yaml <<'YAML'
features:
  - feature_id: F-001
    phase_id: P-001
    title: Write report
    description: Create report.md in the task workspace.
    expected_behavior:
      - report.md exists
    verification_steps:
      - hungerloop mission status demo-mission-1
    fulfills:
      - VAL-DEMO-001
YAML

cat > demo-mission/validation-contract.yaml <<'YAML'
assertions:
  - assertion_id: VAL-DEMO-001
    phase_id: P-001
    title: Report exists
    description: report.md is present after the mission run.
    check_type: file_contains_regex
    params:
      file: report.md
      pattern: ".+"
YAML

# 2. Create the mission task
hungerloop mission new demo-mission-1 --from demo-mission --goal "Generate a short report"

# 3. Run the mission runtime (keeps v0.5f cost guard / workspace isolation and the v0.6 artifact mirror)
hungerloop mission run demo-mission-1 --max-loops 5

# 4. Open the mission cockpit
hungerloop mission status demo-mission-1
```

Common inspection commands:

```bash
hungerloop mission features demo-mission-1
hungerloop mission validation demo-mission-1
hungerloop report demo-mission-1
```

---

## 5-Minute Quickstart (legacy acceptance checks)

```bash
# 1. Write an acceptance spec
cat > accept.yaml <<'YAML'
core_acceptance_checks:
  - check_type: file_exists
    params: {path: report.md}
    description: report.md must exist
YAML

# 2. Create the task (persisted to ./hungerloop.sqlite)
hungerloop new "generate a small report" --accept-file accept.yaml --task-id demo-1

# 3. Run it (default dummy model: zero cost, reproducible)
hungerloop run demo-1

# 4. Check progress
hungerloop status demo-1

# 5. Read the human-readable report
hungerloop report demo-1

# 6. Export the full event trace (JSONL, ready for Grafana/jq analysis)
hungerloop trace export demo-1 --format jsonl
```

A complete demo task lives in `examples/demo_task.yaml`; the integration test `tests/integration/test_orchestrator_dummy_done.py` demonstrates the same end-to-end wiring as the CLI.

---

## Full Workflow

```
                  ┌──────────────────────────────────────────────────────┐
                  │                Task Lifecycle                        │
                  └──────────────────────────────────────────────────────┘

  hungerloop new ──► [pending]
                      │
                      │  hungerloop run <task_id>
                      ▼
                   [running] ◄──────────────────────────────┐
                      │                                     │
        ┌─────────────┼─────────────┐                       │
        │             │             │                       │
        ▼             ▼             ▼                       │
   Each loop:     ┌────────────────────────────────┐       │
   1. HungerEngine.tick()  → pick stop reason       │       │
   2. RuleBasedPlanner     → pick hunger item       │       │
   3. ExecutionWorker      → call model/tools,      │       │
                             write candidate        │       │
   4. ValidationGate       → target + regression    │       │
                             checks                 │       │
   5. CommitManager        → commit only if I-3     │       │
   6. MemoryManager        → extract/promote        │       │
                             MemoryCandidate        │       │
   7. SkillManager         → grant card on DONE+    │       │
                             ≥2 checks              │       │
                          └────────────────────────────────┘
                      │
                      │  stop condition met
                      ▼
                  [stopped]
                      │
              One of the StopReasons:
              ├─ DONE              → hungerloop report
              ├─ HUNGER_EXPIRED    → hungerloop hunger refill --loops N
              ├─ BLOCKED           → hungerloop hunger unblock <item_id>
              ├─ SAFETY_STOP       → hungerloop run --raise-cost-ceiling
              └─ HUMAN_PAUSED      → hungerloop run --resume
                      │
                      │  after human intervention
                      ▼
                   [running]  ◄────────────── (back to the loop)
```

**Key persistence semantics**:

- All state lives in `./hungerloop.sqlite` (default path); override with `--db /path/to/other.sqlite`.
- Workspace files (`best/`, `candidates/loop_NNN/`) live under `./workspace/<task_id>/` by default, managed by the `WorkspaceManager`.
- The `task_locks` table guarantees only one `hungerloop run` owns a task at a time; after a crash, the lock becomes stealable via `--steal-lock` once it exceeds `HUNGERLOOP_LOCK_STALE_SEC` (default 1800 seconds).

---

## CLI Guide

By default the CLI opens `hungerloop.sqlite` in the **current working directory** (via `_default_context()`). To use another location, `cd` there first; `hungerloop checks` is the only subcommand that accepts an explicit `--db PATH` override (v0.4.1 legacy).

### `hungerloop new` — create a task

```bash
hungerloop new "<goal description>" \
  [--task-id <id>] \
  [--accept '<json check>' ...] \
  [--accept-file <path.yaml|.json>] \
  [--memory-consolidation]
```

- `--task-id`: a UUID is generated when omitted.
- `--accept`: repeatable, a single check in JSON form (`{"check_type":"file_exists","params":{"path":"x.md"}}`).
- `--accept-file`: load from a YAML/JSON file (recommended). See [Acceptance Spec File](#acceptance-spec-file).
- `--memory-consolidation`: enable memory candidate generation.

### `hungerloop run` — run the loop

```bash
hungerloop run <task_id> \
  [--max-loops N] \
  [--model-config model.yaml] \
  [--refill N]                  # feed N loops of hunger right after creation
  [--unblock-all]               # unblock all BLOCKED items
  [--resume]                    # resume from HUMAN_PAUSED
  [--raise-cost-ceiling]        # raise the cost ceiling once (use after SAFETY_STOP)
  [--steal-lock]                # steal a stale lock
  [--lock-stale-sec SEC]        # custom stale threshold (default 1800)
  [--draft-k N]                 # cold-start draft sampling count (first loop only; 1 disables, max 5)
```

**Resume preflight**: before every `run`, the CLI checks the task's current state, the last stop reason, and lock staleness, and persists the results as events. If the state does not allow resuming (e.g. `HUMAN_PAUSED` without `--resume`), the CLI exits with a clear message.

### `hungerloop mission` — v0.7 mission runtime

```bash
hungerloop mission new <task_id> [--goal TEXT] [--from PATH] [--contract PATH]
hungerloop mission run <task_id> [--max-loops N] [--refill N] [--resume] [--reset]
hungerloop mission status <task_id> [--json]
hungerloop mission features <task_id> [--phase PHASE_ID] [--json]
hungerloop mission validation <task_id> [--phase PHASE_ID] [--json]
hungerloop mission edit <task_id>
hungerloop mission import <task_id> --from PATH
```

The 7 mission subcommands cover the full mission lifecycle introduced in v0.6 and extended in v0.7:

- `mission new`: create a mission task from `mission.md` / `features.yaml` / `validation-contract.yaml`; passing `--accept` falls back to the legacy task creation path. In v0.7, `SpecCheckSynthesizer` fills in spec coverage checks.
- `mission run`: run the mission-aware orchestrator; `HUNGERLOOP_MISSION_RUNTIME=0` remains a legacy v0.6 rollback flag for compatibility diagnostics only — the normal v0.7 path does not depend on it.
- `mission status`: show the mission cockpit (phase / feature / validation summary); `--json` prints structured state.
- `mission features`: list the feature queue, filterable by phase.
- `mission validation`: list validation contract assertion status, filterable by phase.
- `mission edit`: open `$EDITOR`; on save, changes are written to SQLite through the import/compiler path.
- `mission import`: explicitly import from artifacts, allowed only in `HUMAN_PAUSED` state; SQLite is the single source of truth, and `best/*.yaml` is regenerated by `MissionStateUpdater` in the commit tail.

### `hungerloop status` — task status

```bash
hungerloop status <task_id>
```

Shows the current phase, the latest hunger snapshot (hunger value, remaining loops, cumulative cost/tokens), the most recent stop_reason, and the accepted check count.

### `hungerloop report` — human-readable report

```bash
hungerloop report <task_id> [--format text|json]
```

- `text` (default): summary + acceptance check table + the last N loop decisions.
- `json`: full structured output, suitable for CI consumption.

### `hungerloop trace export` — export the event trace

```bash
hungerloop trace export <task_id> --format jsonl|json
```

Exports the entire task lifecycle event stream (`loop_started`, `loop_committed`, `loop_rejected`, `safety_stop`, `human_required`, etc.) for offline analysis, alerting pipelines, or regression investigation.

### `hungerloop hunger ...` — hunger operations

```bash
hungerloop hunger refill <task_id> --loops N    # feed N loops
hungerloop hunger unblock <task_id> <item_id>   # unblock a single BLOCKED item
hungerloop hunger unblock-all <task_id>         # unblock all BLOCKED items
hungerloop hunger freeze <task_id>              # freeze (stop consuming hunger)
hungerloop hunger resume <task_id>              # unfreeze
```

### `hungerloop memory list` — list memory candidates

```bash
hungerloop memory list <task_id> [--state candidate|approved|rejected]
```

Shows the candidate memories (facts/procedures/preferences/pitfalls) extracted by the `MemoryManager` and auto-promotable since v0.7, filterable by lifecycle state. Each candidate carries deterministic predicate flags (`action_verified`, `reusable`, `non_volatile`, `traceable`); promoted Layer-3 memories can be recalled by relevance in later tasks.

### `hungerloop skill list` — list skill cards

```bash
hungerloop skill list [<task_id>]
```

Without a task_id, lists all skill cards. A skill card is generated only when a task is `DONE` with at least 2 passing checks.

### `hungerloop workspace ...` — workspace inspection

```bash
hungerloop workspace best <task_id> [--root workspace]
hungerloop workspace candidate <task_id> --loop N [--root workspace]
hungerloop workspace rejected <task_id> --loop N [--root workspace]
```

Lists the files of the best state, a specific loop's candidate, or a rejected candidate in the workspace directory.

### `hungerloop checks` — acceptance check status

```bash
hungerloop checks <task_id> [--db PATH]
```

Shows whether each acceptance check currently passes, the loop that last asserted it, and the associated evidence ids.

### `hungerloop repair-state` — state repair

```bash
hungerloop repair-state <task_id> [--apply] [--scope all|hunger|workspace] [--no-events]
```

Detects divergences between the in-memory model and the SQLite blackboard; dry-run by default. Repairs are written only with `--apply`. Exit codes: 0 = no divergence, 1 = divergence found but not repaired, 2 = repaired.

---

## Acceptance Spec File

`--accept-file` accepts YAML or JSON. The root key `core_acceptance_checks` is an array; each item needs `check_type` and `params`:

```yaml
core_acceptance_checks:
  - check_type: file_exists
    params:
      path: report.md
    description: report.md must exist

  - check_type: shell_exit_zero
    params:
      argv: ["python", "-c", "open('report.md').read(); print('ok')"]
      timeout: 10
    description: report.md must be readable
```

Supported `check_type` values include `file_exists`, `shell_exit_zero`, `http_status`, `regex_match`, and more. See `services/validation_gate.py` for the full list. All shell-type checks run through the `SandboxRunner` (path whitelist + process-group cleanup + enforced timeout).

---

## Model Configuration

`run --model-config model.yaml`:

```yaml
provider: openai            # dummy | openai | azure_openai (azure is a placeholder)
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY # environment variable name only; no plaintext keys
base_url: null              # optional custom endpoint
pricing:
  input_per_1k_usd: 0.00015
  output_per_1k_usd: 0.0006
retry:
  max_attempts: 3
  initial_backoff_sec: 1.0
```

**Security rule (enforced)**: YAML must not contain a plaintext `api_key:`; only `api_key_env: <ENV_VAR_NAME>` is allowed. Azure OpenAI remains a placeholder runtime and raises explicitly when called.

The `dummy` provider needs no key and is used for deterministic local regression.

---

## Invariants Reference

| ID | Name | Implementation |
| -- | ---- | -------------- |
| I-3 | Check-level commits, never score-based; ADR-010 allows only policy-gated, time-boxed, declarative refactor transaction waivers | `commit_manager.py`, `hunger_update.py` |
| I-4 | Workspace isolation: only the `CommitManager` writes `best/` | `workspace_manager.py` |
| I-5 | Targeted validation + regression: previously passing checks are re-tested | `validation_gate.py` |
| I-6 | Stagnation detection counts only `attempted` items; the global fuse threshold is policy-configurable (`max_global_no_progress_loops`, default 5), and rejected candidates increment the streak even with raw newly-passed checks | `stagnation_detector.py` |
| I-7 | Sandbox isolation: path whitelist + process-group cleanup | `sandbox_runner.py`, `path_safety.py` |
| I-8 | Cost guard: budget checked before and after every call | `cost_guard.py` |
| I-9 | `BLOCKED ≠ DONE`; strict stop-reason priority | `hunger_engine.py` |
| I-10 | The hunger ledger is compiled from policy | `requirement_compiler.py` |

Violating any of these is a regression, not a refactor. See `CLAUDE.md` for details.

---

## Project Structure

```
src/hungerloop/
  models/         # frozen Pydantic snapshot models; do not add mutable methods
  services/       # stateless services; repo access via DI
    mission_planner.py       # v0.6 mission feature → assignment planner
    worker_scheduler.py      # v0.6 sequential topology executor
    handoff_processor.py     # v0.6 structured handoff → ledger compiler path; v0.7 proposed checks/discovery credit
    validation_pipeline.py   # deterministic + scrutiny + user-testing stages
    mission_state_updater.py # SQLite → best/mission.md|features.yaml|validation-contract.yaml|services.yaml mirror
    ...                      # v0.7 spec-to-check synthesis, refactor transaction, memory recall services
    validators/              # deterministic/scrutiny/user-testing validators
  repository/     # Protocol + InMemoryRepository + SQLiteRepository + migrations
    migrations/   # v1__initial.sql, v2__memory_candidate_lifecycle.sql,
                  # v3__sqlite_runtime_tables.sql, v4__memory_candidate_sources.sql
  cli/            # click entry points: new, run, status, report, trace,
                  # hunger, memory, skill, workspace, checks, repair-state
tests/
  unit/           # unit tests
  integration/    # end-to-end orchestrator tests
examples/
  demo_task.yaml  # minimal legacy task example
specs/            # PRD and per-version implementation specs
docs/
  architecture/   # architecture diagrams and decision records
  superpowers/    # implementation plan archive
```

---

## Development & Testing

```bash
# Full test suite (v0.7 final gate)
pytest tests/

# Strict type checking (must be zero errors)
mypy --strict src/

# Lint
ruff check src/ tests/

# CLI smoke test
hungerloop --version
hungerloop new "smoke test" --accept-file examples/demo_task.yaml --task-id smoke
hungerloop run smoke
```

Latest final gate results: `pytest` 1747 passed / 1 skipped / 20 approved Windows baseline deselected; `mypy --strict src/` 104 files clean; `ruff check src/ tests/` clean; CLI smoke passed. The 20 deselections are an approved Windows baseline filter and contain no secrets or persistent services.

v0.7.1-v0.7.2 branch gate record (verified with commit `0fb5e02`): `pytest` 1912 passed / 12 known Windows env failures (identical on the pristine branch); `mypy --strict` 104 files clean; `ruff` clean.

**Key conventions**:

- Every module starts with `from __future__ import annotations`.
- Public APIs are fully typed; use `X | None` instead of `Optional[X]`.
- Models use Pydantic v2, but the `pydantic.mypy` plugin is **disabled** (incompatible with mypy ≥1.18); do not re-enable it.
- I/O- or subprocess-related service methods are `async`; `pytest-asyncio` runs in `auto` mode.
- Commit style: `feat:`, `fix:`, `docs:`, `refactor:`, `test:` — see `git log`.
- Database migrations are **forward-only**: never modify a published vN.sql file; only append vN+1.sql (PRD §5.5).

---

## Docs & Roadmap

**Docs**:

- `specs/PRD/hungerloop_v0_6_prd.md` — v0.6 product requirements and the ADR-007/008/009 convergence wording
- `specs/v0.6_implementation/` — EARS implementation specs for M1..M6 + RC
- `specs/v0.7_implementation/` — Loop-Objective Evolution implementation specs and final scope
- `docs/architecture/v0.6/adr/` — v0.6 architecture decision records
- `docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md` — bounded refactor transaction decision
- `CLAUDE.md` — invariants, conventions, MCP tool usage
- `RELEASE_CHECKLIST.md` — pre-release verification steps

**Roadmap**:

- **v0.7.x**: Loop-Objective Evolution hardening, Windows baseline maintenance, v0.6 mission runtime compatibility fixes. Already landed on the `v0.7.1-v0.7.2` branch: draft sampling, policy-configurable global fuse, ADR-010 auto-open, worker recovery simplification with fixed-k sampling preserved
- **v0.8+**: `LLMPlanner`, true concurrent fan-out + join, `services.yaml` rich semantics, Web UI

---

## License

MIT
