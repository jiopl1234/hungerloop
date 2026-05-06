# HungerLoop — Claude Code Project Notes

Python 3.11+ async agent harness. Current shipped version: **v0.4.1** (MVP).
Next milestone PRD: `HungerLoop_Next_Step_PRD.md` (v0.5).

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
  models/       # Pydantic models — frozen snapshots. Do not add mutating methods.
    enums.py, hunger.py, blackboard.py, planning.py, validation.py,
    context.py, tracing.py, worker.py, workspace.py
  services/     # Stateless services; all take repo via DI.
    hunger_engine.py, hunger_update.py, cost_guard.py, validation_gate.py,
    commit_manager.py, integrator.py, context_builder.py,
    workspace_manager.py, sandbox_runner.py, acceptance_runner.py,
    stagnation_detector.py, requirement_compiler.py, path_safety.py
  repository/   # Protocol + InMemoryRepository (no SQLite yet)
  cli/          # click-based: workspace, checks
tests/unit/     # 89 tests, all green on main
docs/superpowers/plans/   # implementation plans
```

## Invariants — DO NOT VIOLATE

These are encoded in code and tests. Breaking one is a regression, not a refactor.

- **I-3 Check-level commits.** Promotion to `best/` requires `newly_passed_check_keys` ≠ ∅, no regressions, evidence present. Score-based commits are forbidden — `BestState.score` exists for schema only and stays at 0.0.
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
pytest tests/                       # 89 tests
mypy --strict src/                  # must be clean
ruff check src/ tests/
hungerloop --version                # CLI smoke
hungerloop workspace inspect <task_id>
hungerloop checks status <task_id>
```

- **Python:** 3.11+, `from __future__ import annotations` in every module.
- **Types:** `mypy --strict` is non-negotiable. Public APIs fully annotated. Use `X | None`, not `Optional[X]`.
- **Async:** `pytest-asyncio` in `auto` mode. Service methods that touch I/O or subprocesses are async.
- **Models:** Pydantic v2. The `pydantic.mypy` plugin is **disabled** (incompatible with mypy ≥1.18); see `pyproject.toml` note. Don't re-enable without checking versions.
- **Repository typing:** Several services still take `repo: Any` with `TODO(Task 14)`. When tightening, use `RepositoryProtocol` from `repository/protocol.py`.

## Conventions

- **Commit style:** `feat:`, `fix:`, `docs:`, `refactor:`, `test:` (see `git log`).
- **No score-based logic in commits, hunger updates, or validation.** If you find yourself reaching for `score`, you are doing the wrong thing — re-read I-3.
- **Frozen models.** Models are data containers; behavior lives in services. Don't add `def apply()` to a `BaseModel`.
- **Evidence is mandatory.** A candidate with no evidence_ids cannot commit (I-3). Sandbox runs auto-emit evidence; LLM/tool wrappers must do the same.

## Things in flight / known gaps

- Repository is `InMemoryRepository` only — no persistence layer yet.
- No Orchestrator, no Worker implementations, no `ModelClient` — all v0.5 work.
- Several services have `TODO(Task 14)` markers for repo-protocol typing.
- `gap_score` decrement is fractional (`hunger_update.py:48`); equality check at zero may need an epsilon when extending.
