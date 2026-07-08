# HungerLoop — Claude Code Project Notes

Python 3.11+ async agent harness. Current shipped version: **v0.6.0** (mission runtime).
Canonical v0.6 PRD: `specs/PRD/hungerloop_v0_6_prd.md`.

## Code search — use MCP tools, not grep/Explore

When locating symbols, definitions, references, or doing semantic lookups, **prefer these MCP tools** over `grep`, `find`, or the `Explore` agent:

- **`serena`** (LSP-backed, primary):
  - `mcp__serena__find_symbol` — locate a class/function by name path (e.g. `HungerEngine/tick`)
  - `mcp__serena__get_symbols_overview` — map a file's top-level symbols
  - `mcp__serena__find_referencing_symbols` — who calls/imports X
  - `mcp__serena__find_declaration` / `find_implementations` — go-to-def / impls
  - `mcp__serena__replace_symbol_body` — surgical edits at symbol granularity
  - On first use in a session, call `mcp__serena__initial_instructions` once.
- **`ace-tools`** (semantic retrieval, secondary):
  - `mcp__ace-tools__codebase-retrieval` — natural-language "where is the logic that does X" queries.

Fall back to `Bash` `grep` / `Read` only when the MCP tools are unavailable or when you need raw text matching (e.g. searching docs/markdown).

## Layout

```
src/hungerloop/
  models/       # Pydantic v2 data models. Do not add mutating business methods.
    enums.py, hunger.py, blackboard.py, planning.py, validation.py,
    context.py, tracing.py, worker.py, workspace.py
    mission.py, validation_contract.py, handoff.py
  services/     # Stateless services; all take repo via DI.
    hunger_engine.py, hunger_update.py, cost_guard.py, validation_gate.py,
    commit_manager.py, integrator.py, context_builder.py,
    workspace_manager.py, sandbox_runner.py, acceptance_runner.py,
    stagnation_detector.py, requirement_compiler.py, path_safety.py,
    services/mission_planner.py, services/worker_scheduler.py,
    services/handoff_processor.py, services/mission_state_updater.py,
    validation_pipeline.py, mission_loader.py,
    validators/  # deterministic/scrutiny/user-testing; no ModelClient imports
  repository/   # Protocol + InMemoryRepository + SQLiteRepository + migrations
  cli/          # click-based: new/run/status/report/trace/mission/etc.
tests/unit/     # v0.6 baseline: ≥761 unit tests
tests/integration/ # v0.6 baseline: ≥19 integration tests collected
docs/architecture/v0.6/adr/ # ADR-007/008/009 accepted decisions
docs/architecture/v0.7/adr/ # ADR-010 refactor transactions
```

## Invariants — DO NOT VIOLATE

These are encoded in code and tests. Breaking one is a regression, not a refactor.

- **I-3 Check-level commits.** Promotion to `best/` requires `newly_passed_check_keys` ≠ ∅, no regressions, evidence present. Commits using score as a decision factor are forbidden — `BestState.score` exists for schema only and stays at 0.0. **ADR-010 exception:** when a matching open refactor transaction is active (policy-gated, `refactor_transactions_enabled=True`), `CommitManager` tolerates regressions only for check keys declared in the transaction `declared_regression_keys`, bounded by a deadline and rolled back on failure. See `docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md`. Closed, rolled-back, wrong-task, or stale transactions do not relax I-3. This is the only approved I-3 amendment.
- **I-4 Workspace isolation.** Workers/agents read `best/`, write to `candidates/loop_NNN/`. Only `CommitManager` promotes. Never modify `best/` directly.
- **I-5 Targeted validation.** `ValidationGate` runs target items + previously-passed checks (regression detection). Untested checks stay passed; never re-run all checks blindly.
- **I-6 Stagnation: attempted-only.** `StagnationDetector` only counts items in `attempted_hunger_item_ids`.
- **I-7 Sandbox isolation.** Subprocesses go through `SandboxRunner` (timeout, output cap, process-group cleanup). All paths validated via `path_safety.py`.
- **I-8 Cost ceiling.** `CostGuard.assert_within_budget()` is called **before and after** every LLM/tool invocation.
- **I-9 BLOCKED ≠ DONE.** `HungerEngine.tick()` checks `all_remaining_items_blocked()` before `is_done()`. PAUSED is intentionally distinct from BLOCKED (different stop reasons, different recovery).
- **I-10 Rule-based requirement compilation.** Hunger ledger is generated from policy, not hand-rolled.

`StopReason` priority order (in `HungerEngine.tick`):
`HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`.

## Toolchain & commands

```bash
pip install -e ".[dev]"
pytest tests/                       # v0.6 baseline: ≥761 unit + ≥19 integration collected
mypy --strict src/                  # must be clean
ruff check src/ tests/
hungerloop --version                # CLI smoke
hungerloop workspace inspect <task_id>
hungerloop checks status <task_id>
hungerloop mission --help           # v0.6 mission CLI surface
```

- **Python:** 3.11+, `from __future__ import annotations` in every module.
- **Types:** `mypy --strict` is non-negotiable. Public APIs fully annotated. Use `X | None`, not `Optional[X]`.
- **Async:** `pytest-asyncio` in `auto` mode. Service methods that touch I/O or subprocesses are async.
- **Models:** Pydantic v2. The `pydantic.mypy` plugin is **disabled** (incompatible with mypy ≥1.18); see `pyproject.toml` note. Don't re-enable without checking versions.
- **Repository typing:** Several services still take `repo: Any` with `TODO(Task 14)`. When tightening, use `RepositoryProtocol` from `repository/protocol.py`.
- **CI lint rules:** no LLM under `services/validators/`; no `ModelClient` imports under `services/validators/`; no `yaml.load*` in `mission_state_updater.py`; no repository save/update/delete writes inside `mission_state_updater.py`; no direct ledger writes outside compilers (`requirement_compiler.py` / `refinement_compiler.py`).
- **Mission runtime rollback flag:** `HUNGERLOOP_MISSION_RUNTIME=0` is **DEPRECATED, removable in v0.7.0**. Keep only for RC/v0.6 rollback compatibility.

## Conventions

- **Commit style:** `feat:`, `fix:`, `docs:`, `refactor:`, `test:` (see `git log`).
- **No logic using score in commits, hunger updates, or validation.** If you find yourself reaching for `score`, you are doing the wrong thing — re-read I-3.
- **Models are data containers.** Behavior lives in services. Don't add `def apply()` to a `BaseModel`.
- **Evidence is mandatory.** A candidate with no evidence_ids cannot commit (I-3). Sandbox runs auto-emit evidence; LLM/tool wrappers must do the same.
- **Mission artifacts are mirrors.** SQLite is the single source of truth; `MissionStateUpdater` regenerates `best/mission.md`, `best/features.yaml`, `best/validation-contract.yaml`, and `best/services.yaml` after successful commits. Manual changes go through `hungerloop mission edit/import`.

## Things in flight / known gaps

- `HUNGERLOOP_MISSION_RUNTIME=0` remains as a deprecated v0.6 rollback valve and should be removed in v0.7.0.
- True concurrent fan-out/join, full LLMPlanner, richer `services.yaml`, and Web UI remain v0.7+ placeholders. Cross-task memory recall, spec synthesis, and refactor transactions are delivered in v0.7 (see `specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md`).
