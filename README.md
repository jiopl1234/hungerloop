# HungerLoop v0.4.1

A Python async agent harness implementing check-level progress tracking, workspace isolation, and cost guards for iterative agent loops.

## Status

✅ **MVP Complete** — 89 tests passing, mypy --strict clean, all invariants encoded

## Features

- **Check-level commits (I-3):** No score-based commits — only check-level progress
- **Workspace isolation (I-4):** Copy-on-write candidate workspaces
- **Targeted validation (I-5):** Only validate specified hunger items
- **Stagnation detection (I-6):** Attempted-only failure tracking
- **Sandbox isolation (I-7):** Path safety + process-group cleanup on timeout
- **Cost ceiling (I-8):** Pre/post call budget enforcement
- **BLOCKED ≠ DONE (I-9):** Explicit BLOCKED state, checked before DONE
- **Requirement compilation (I-10):** Rule-based hunger ledger generation

## Quick Start

```bash
# Install
pip install -e .

# Run tests
pytest tests/

# CLI
hungerloop --version
hungerloop workspace best <task_id>
```

## Architecture

- **Models:** 11 Pydantic models (frozen snapshots for validation/blackboard)
- **Services:** 14 services (workspace, sandbox, commit, validation, etc.)
- **Repository:** Protocol + in-memory implementation
- **CLI:** Workspace inspection and check status commands

## Documentation

- `VERIFICATION.md` — Full verification report with invariant coverage matrix
- `docs/superpowers/plans/2026-05-01-hungerloop-v041.md` — Implementation plan
- `HungerLoop_MVP_PRD_v0.4.1_engineering_fix.md` — Product requirements

## License

MIT
