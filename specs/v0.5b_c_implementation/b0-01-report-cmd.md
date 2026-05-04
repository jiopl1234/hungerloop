# b0-01 · `hungerloop report` JSON+markdown formatter

**Spec**: §1 of `v0.5b_c_prd_enhancements.spec.md`. **PRD**: §18.3. **Release**: v0.5b.0.

## Goal

Provide a stable machine-readable task summary so downstream tools (CI dashboards, PR commenters, notebooks) don't have to scrape `hungerloop status` text.

## Files to touch

- **NEW** `src/hungerloop/cli/report_cmd.py` — click command entry point.
- **NEW** `src/hungerloop/cli/report_format.py` — JSON v1 builder + markdown template.
- `src/hungerloop/cli/main.py` — register the new command.
- **NEW** `tests/unit/test_cli_report.py`.

## Checklist

### Schema builder (`report_format.py`)

- [ ] Define `REPORT_SCHEMA_VERSION = "1"` as a module constant.
- [ ] `build_report_dict(repo, task_id) -> dict`: queries the repo for ledger / usage / stop_report / best_state / loop traces, returns the JSON-shaped dict from spec §1.2.
- [ ] Fields populated even when empty: `accepted_check_keys: []`, `last_loop: null`, `stop.reason: null` (running task case).
- [ ] `format_markdown(report_dict) -> str`: distinct template (per decision §1 — does NOT call status_format).
  - Headline: `# Task <id> — <stop.reason>` (or `running` if null).
  - Sections: Goal, Stop, Ledger summary, Usage, Loops, Last loop delta_summary.
- [ ] Both formatters tolerate missing optional pieces (no best_state, no last_loop).

### CLI command (`report_cmd.py`)

- [ ] `@click.command("report")`, `@click.argument("task_id")`, `@click.option("--format", type=click.Choice(["json", "markdown"]), default="json")`.
- [ ] Resolves `CliContext` via `click.pass_obj` like other commands.
- [ ] Task missing → exit code 1, stderr `Task not found: <task_id>`.
- [ ] Otherwise prints to stdout, exit 0.

### Wiring (`main.py`)

- [ ] `from hungerloop.cli.report_cmd import report` and `cli.add_command(report)`.

### Tests (`test_cli_report.py`)

- [ ] `test_report_emits_schema_version_1`
- [ ] `test_report_running_task_has_null_stop_reason`
- [ ] `test_report_done_task_includes_accepted_check_keys`
- [ ] `test_report_unknown_task_exits_1_with_stderr`
- [ ] `test_report_markdown_contains_headline_and_usage`
- [ ] `test_report_markdown_does_not_match_status_byteforbyte` (regression: ensure they didn't accidentally share)
- [ ] `test_report_json_round_trips_through_json_loads`

## Done when

- [ ] All 7 tests pass.
- [ ] `mypy --strict` clean.
- [ ] `hungerloop report demo-1 | jq .schema_version` prints `"1"` against the in-memory demo fixture.
- [ ] PRD §18.3 references this implementation.

## Notes / non-goals

- No YAML output. JSON is the wire contract; YAML is the input language.
- No streaming pagination — task summaries are bounded.
- Schema bumps are a separate task (post-v0.5b).
