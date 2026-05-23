# HungerLoop v0.6.0 — Release Checklist

Release-candidate verification per `specs/PRD/hungerloop_v0_6_prd.md` §14/§15 and RC requirements `REQ-RC-040..045`, `REQ-RC-050..051`.

## v0.6 signed release entry

- [x] Version bump: `pyproject.toml` declares `version = "0.6.0"` and `.venv/bin/hungerloop --version` prints `0.6.0`.
- [x] Test count delta pre/post RC: pre-RC baseline was ≥761 unit + ≥19 integration collected; post-RC baseline verification on 2026-05-23 collected the v0.6 suite and passed `1099 passed, 1 skipped`.
- [x] Migration verification record: one production-equivalent v5 SQLite task DB was migrated through the v6 migrator during RC validation; `PRAGMA user_version` reached `6`, legacy table row counts were preserved, and a `MIGRATION_APPLIED` event recorded `from_version=5`, `to_version=6`, `duration_ms`.
- [x] ADR acceptance dates: ADR-007, ADR-008, and ADR-009 accepted on 2026-05-23.
- [x] Rollback dry-run record: `repository/migrations/v6_rollback.sql` was applied to one staging v6 DB during RC validation; the five v6 mission tables were dropped and `PRAGMA user_version` returned to `5`.
- [x] Deprecated rollback flag: `HUNGERLOOP_MISSION_RUNTIME=0` is **DEPRECATED, removable in v0.7.0**. It is retained only as a v0.6 emergency legacy-path switch and must not be used for new behavior.
- signed-off-by: HungerLoop RC release captain <release-captain@hungerloop.local> (2026-05-23)

## Final validation commands

- [ ] `.venv/bin/pytest -q`
- [ ] `.venv/bin/mypy --strict src/`
- [ ] `.venv/bin/ruff check src/ tests/`
- [ ] `.venv/bin/hungerloop --version`
- [ ] `grep -E '^Status: Accepted' docs/architecture/v0.6/adr/ADR-00{7,8,9}-*.md`

## Documentation freeze

- [x] `README.md` includes v0.6 quickstart, mission runtime feature matrix entry, and all 7 mission subcommands.
- [x] `CLAUDE.md` references `services/mission_planner.py`, `services/worker_scheduler.py`, `services/handoff_processor.py`, `services/mission_state_updater.py`, `services/validators/`, the v0.6 CI lint rules, and the ≥761 unit + ≥19 integration baseline.
- [x] `AGENTS.md` references the same mission services, CI lint rules, artifact single-source-of-truth rule, and baseline.
- [x] `specs/PRD/hungerloop_v0_6_prd.md` adopts ADR-007/008/009 wording and RC baseline updates.
- [x] ADR-007/008/009 status headers are `Accepted (2026-05-23)`.
- [x] v0.7 placeholders exist under `specs/v0.7_placeholders/`.

## v0.6 acceptance checklist

- [x] Mission runtime models, repository v6 schema, planner/scheduler, handoff processor, validation pipeline, mission artifact regeneration, mission CLI, and report cockpit are implemented.
- [x] Invariants I-3..I-10 have explicit regression coverage.
- [x] `HUNGERLOOP_MISSION_RUNTIME=0` legacy rollback path is tested and emits a deprecation warning.
- [x] No LLM judge is used in validators; validator CI lint forbids `ModelClient` imports.
- [x] `MissionStateUpdater` is one-way SQLite → artifact projection and does not use `yaml.load*` or repository save/update/delete calls.
