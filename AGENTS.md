# Repository Guidelines

## Project Structure & Module Organization

HungerLoop is a Python 3.11+ async agent harness. Source code lives in `src/hungerloop/`:

- `models/`: Pydantic v2 data models and enums.
- `services/`: stateless business logic such as orchestration, validation, budgeting, memory, workspace handling, and v0.6 mission runtime services:
  - `services/mission_planner.py`: rule-based mission feature → assignment planning.
  - `services/worker_scheduler.py`: sequential topology execution with shared candidate workspace.
  - `services/handoff_processor.py`: structured handoff routing through compiler-owned ledger updates.
  - `services/mission_state_updater.py`: SQLite → `best/*.yaml` / `best/mission.md` mirror regeneration.
  - `services/validators/`: deterministic, scrutiny, and user-testing validator stages.
- `repository/`: `RepositoryProtocol`, in-memory and SQLite implementations, migrations.
- `cli/`: Click-based CLI commands.

Tests are under `tests/unit/`, `tests/integration/`, and opt-in performance tests under `tests/perf/`. Specs and planning docs live in `specs/` and `docs/`.

## Build, Test, and Development Commands

- `pip install -e ".[dev]"`: install the package with development dependencies.
- `pytest tests/`: run unit and integration tests.
- `mypy --strict src/`: run strict type checking.
- `ruff check src/ tests/`: run lint checks.
- `hungerloop --version`: smoke-test the installed CLI.
- `hungerloop mission --help`: smoke-test the v0.6 mission CLI group.

Run focused tests during iteration, then run the full validation set before finishing changes.
The v0.6 release baseline is **≥761 unit + ≥19 integration tests collected**; the default suite may skip the real-LLM integration test unless explicitly enabled.

## Coding Style & Naming Conventions

Use `from __future__ import annotations` in every Python module. Prefer `X | None` over `Optional[X]`. Keep public APIs fully typed and compatible with `mypy --strict`.

Models are data containers; business behavior belongs in `services/`. Services should receive dependencies through dependency injection, usually via `RepositoryProtocol`. Do not introduce score-based commit logic.

CI lint rules for v0.6 are part of the contract: no `ModelClient` imports under `services/validators/`; no `yaml.load*` in `mission_state_updater.py`; no repository save/update/delete calls inside `mission_state_updater.py`; and no direct ledger writes outside compilers (`requirement_compiler.py` / `refinement_compiler.py`).

## Testing Guidelines

Use pytest with `pytest-asyncio` in auto mode. Name tests `test_*.py` and test functions `test_*`. Add or update tests for changes to CLI, repository, orchestrator, persistence, budget/refinement, memory/skill, or repair-state behavior.

Preserve key invariants: check-level commits, workspace isolation, targeted validation, attempted-only stagnation, sandbox/path safety, cost guards, and `BLOCKED != DONE`.
Mission artifacts are read-only mirrors: SQLite is the single source of truth and `MissionStateUpdater` regenerates `best/mission.md`, `best/features.yaml`, `best/validation-contract.yaml`, and `best/services.yaml` after successful commits. Manual mission changes must go through `hungerloop mission edit` or `hungerloop mission import`.

## Commit & Pull Request Guidelines

Commit history uses concise prefixes such as `feat:`, `fix:`, `docs:`, `refactor:`, and `test:`. Keep messages focused on the user-visible reason for the change.

Before committing, inspect `git status`, `git diff`, and staged diffs for secrets or unrelated local artifacts. Do not include `.serena/logs/`, caches, build outputs, or environment files. PRs should describe the change, list validations run, and call out any invariant-sensitive areas.

## Agent-Specific Instructions

Prefer Serena MCP symbol tools for code navigation and surgical edits. Keep `.serena/project.yml` configured for Python LSP support. Never modify `best/` workspaces directly; candidate promotion must go through `CommitManager`.
`HUNGERLOOP_MISSION_RUNTIME=0` is **DEPRECATED, removable in v0.7.0** and exists only as a v0.6 rollback valve; do not build new behavior around it.
