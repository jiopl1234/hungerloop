# HungerLoop v0.5a

A Python async agent harness implementing check-level progress tracking, hunger-based budgets, workspace isolation, and cost guards for iterative agent loops.

## Status

**v0.5a — Orchestrator + Dummy ExecutionWorker.** 310 tests green, `mypy --strict src/` clean, all v0.4 invariants preserved.

The v0.5a release adds the loop orchestrator, planner, worker runtime, model client, tool harness, memory candidate generation, skill card trigger, and CLI. Production persistence (`SQLiteRepository`) ships in v0.5b.

## What's new in v0.5a

- **LoopOrchestrator** drives the full hunger → plan → execute → validate → commit cycle (PRD §12).
- **RuleBasedPlanner** picks the highest `priority × gap_score` item and routes to `execution_worker_v1` (§5).
- **WorkerRuntime + ExecutionWorker** with `BudgetGuard`, side-effect gating, and `ToolNotPermitted` errors (§6, §7, §28.11).
- **ModelClient + DummyModelClient + OpenAIModelClient** with retry, JSON safety, `Retry-After`, and final-error evidence (§11.4 / §28.2 / §28.3).
- **ModelConfig + PricingTable** with YAML safety rules (no plaintext API keys, env-only, Azure deferred to v0.5b) (§10, §11.3).
- **MemoryManager** generates `MemoryCandidate` rows per loop with deterministic predicates (`action_verified`, `reusable`, `non_volatile`, `traceable`) (§19).
- **SkillManager** emits a `SkillCard` only on `DONE` + ≥2 accepted checks (§20).
- **CLI**: `new`, `run` (with resume preflight), `status`, `hunger {refill,unblock,unblock-all,freeze,resume}`, `memory list`, `skill list` (§18).

## Invariants

| ID | Name | Where it lives |
| -- | ---- | -------------- |
| I-3 | Check-level commits, never score | `commit_manager.py`, `hunger_update.py` |
| I-4 | Workspace isolation: only `CommitManager` writes `best/` | `workspace_manager.py` |
| I-5 | Targeted validation; previously-passed checks re-run for regressions | `validation_gate.py` |
| I-6 | Stagnation counts attempted-only items | `stagnation_detector.py` |
| I-7 | Sandbox isolation: path safety + process-group cleanup | `sandbox_runner.py`, `path_safety.py` |
| I-8 | Cost ceiling enforced pre and post every call | `cost_guard.py` |
| I-9 | `BLOCKED ≠ DONE`; ordered `HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE` | `hunger_engine.py` |
| I-10 | Hunger ledger compiled from policy | `requirement_compiler.py` |

## Quick start

```bash
pip install -e ".[dev]"

# Tests
pytest tests/                      # 310 tests
mypy --strict src/                 # 60 source files, clean
ruff check src/ tests/

# CLI smoke (production: requires SQLiteRepository, which ships in v0.5b)
hungerloop --version
```

## Demo task

The deterministic demo task lives at `examples/demo_task.yaml`:

```yaml
goal: "Create a small report and validate it."
policy:
  initial_hunger: 100
  max_total_cost_usd: 1.0
  max_total_tokens: 100000
acceptance:
  core_acceptance_checks:
    - check_type: file_exists
      params: {path: report.md}
      description: report.md exists
    - check_type: shell_exit_zero
      params:
        argv: ["python", "-c", "open('report.md').read(); print('ok')"]
        timeout: 10
      description: report.md is readable
```

Run end-to-end with the dummy model client by inspecting `tests/integration/test_orchestrator_dummy_done.py` — it wires `build_orchestrator` exactly as the CLI does.

## Architecture

```
src/hungerloop/
  models/         # Pydantic models — frozen snapshots
  services/       # Stateless services; all take repo via DI
  repository/     # Protocol + InMemoryRepository (SQLite ships in v0.5b)
  cli/            # click-based: new, run, status, hunger, memory, skill, workspace, checks
tests/
  unit/           # 280 unit tests
  integration/    # 6 end-to-end orchestrator tests (PRD §23.2)
```

## Documentation

- `hungerloop_v0_5_2_prd.md` — v0.5.2 product requirements (current source of truth)
- `HungerLoop_MVP_PRD_v0.4.1_engineering_fix.md` — v0.4.1 baseline
- `CLAUDE.md` — invariants, conventions, MCP tool usage
- `RELEASE_CHECKLIST.md` — pre-release verification steps

## Roadmap

- **v0.5b** — `SQLiteRepository`, real `OpenAIModelClient` end-to-end, `--model-config` CLI flag.
- **v0.5c** — Memory promotion CLI, additional skill triggers.
- **v0.5d** — `LearningWorker`, `ResearchWorker`.
- **v0.6** — `LLMPlanner`, multi-worker assignments, optional parallel execution.

## License

MIT
