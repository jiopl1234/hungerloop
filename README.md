# HungerLoop v0.5b/c

A Python async agent harness implementing check-level progress tracking, hunger-based budgets, workspace isolation, and cost guards for iterative agent loops.

## Status

**v0.5b/c — SQLite-backed CLI + trace/report hardening.** The default CLI opens `hungerloop.sqlite`, supports durable dummy runs across processes, and preserves all v0.4 invariants.

Implemented surfaces:

- `SQLiteRepository` with forward-only migrations, WAL mode, usage snapshots, task locks, events, traces, reports, memory candidates, and skill cards.
- CLI `new`, `run`, `status`, `report`, `trace export`, `hunger`, `memory`, `skill`, `workspace`, and `checks` over the default SQLite context.
- `new --accept-file accept.yaml` for YAML/JSON acceptance specs.
- `run --model-config model.yaml` for `dummy` and `openai` providers; `azure_openai` fails clearly until shipped.
- Loop lifecycle events (`loop_started`, `loop_committed`, `loop_rejected`, `safety_stop`, `human_required`) for trace export.

Still deferred:

- Long-term production memory promotion workflows.
- `LearningWorker`, `ResearchWorker`, `LLMPlanner`, and multi-worker assignment.
- Azure OpenAI runtime.

## What's Included

- **LoopOrchestrator** drives the full hunger → plan → execute → validate → commit cycle (PRD §12).
- **RuleBasedPlanner** picks the highest `priority × gap_score` item and routes to `execution_worker_v1` (§5).
- **WorkerRuntime + ExecutionWorker** with `BudgetGuard`, side-effect gating, and `ToolNotPermitted` errors (§6, §7, §28.11).
- **ModelClient + DummyModelClient + OpenAIModelClient** with retry, JSON safety, `Retry-After`, and final-error evidence (§11.4 / §28.2 / §28.3).
- **ModelConfig + PricingTable** with YAML safety rules (no plaintext API keys, env-only, Azure explicitly deferred) (§10, §11.3).
- **MemoryManager** generates `MemoryCandidate` rows per loop with deterministic predicates (`action_verified`, `reusable`, `non_volatile`, `traceable`) (§19).
- **SkillManager** emits a `SkillCard` only on `DONE` + ≥2 accepted checks (§20).
- **CLI**: `new`, `run` (with resume preflight), `status`, `report`, `trace export`, `hunger {refill,unblock,unblock-all,freeze,resume}`, `memory list`, `skill list` (§18).

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
pytest tests/
mypy --strict src/
ruff check src/ tests/

# CLI smoke
hungerloop --version
cat > accept.yaml <<'YAML'
core_acceptance_checks:
  - check_type: file_exists
    params: {path: report.md}
    description: report.md exists
YAML
hungerloop new "Create a small report" --accept-file accept.yaml --task-id demo-1
hungerloop run demo-1
hungerloop status demo-1
hungerloop report demo-1
hungerloop trace export demo-1
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
  repository/     # Protocol + InMemoryRepository + SQLiteRepository
  cli/            # click-based: new, run, status, report, trace, hunger, memory, skill, workspace, checks
tests/
  unit/           # unit tests
  integration/    # end-to-end orchestrator tests
```

## Documentation

- `hungerloop_v0_5b_c_prd.md` — v0.5b/c product requirements
- `hungerloop_v0_5_2_prd.md` — v0.5.2 product requirements
- `HungerLoop_MVP_PRD_v0.4.1_engineering_fix.md` — v0.4.1 baseline
- `CLAUDE.md` — invariants, conventions, MCP tool usage
- `RELEASE_CHECKLIST.md` — pre-release verification steps

## Roadmap

- **v0.5c** — Memory promotion CLI, additional skill triggers.
- **v0.5d** — `LearningWorker`, `ResearchWorker`.
- **v0.6** — `LLMPlanner`, multi-worker assignments, optional parallel execution.

## License

MIT
