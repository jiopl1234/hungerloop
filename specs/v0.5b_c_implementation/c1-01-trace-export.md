# c1-01 · `hungerloop trace export` + perf budget

**Spec**: §7 (export part) + §9 (perf NFR). **PRD**: §22.8 + §9 (cross-cutting). **Release**: v0.5c.1.

## Goal

Stream a task's events as JSONL to stdout so external tools (Prometheus exporters, dashboards, log shippers) have a stable interface. Pin `report` and `status` to a < 200 ms perf budget for typical task sizes.

## Files to touch

- `src/hungerloop/cli/trace_cmd.py` (extend if it exists; create if not) — add `export` subcommand.
- `src/hungerloop/cli/main.py` — register if new.
- **NEW** `tests/unit/test_trace_export.py`.
- **NEW** `tests/perf/test_report_status_perf.py` (new directory `tests/perf/`).

## Checklist

### `trace export` command

- [ ] `@click.command("export")`, `@click.argument("task_id")`, `@click.option("--format", type=click.Choice(["jsonl"]), default="jsonl")`.
  (Single format option for now; preserves room for `--format prometheus` later without changing the command shape.)
- [ ] Read all events for the task in chronological order.
- [ ] Emit one JSON object per line, no trailing newline at EOF beyond the last line's `\n`.
- [ ] Each line schema:
  ```json
  {"task_id":"t1","loop_id":3,"event_type":"loop_committed","payload":{...},"created_at":"2026-05-04T13:22:09Z"}
  ```
- [ ] If task doesn't exist: stderr "Task not found", exit 1.
- [ ] If task has zero events: empty stdout, exit 0.

### Perf test (`tests/perf/test_report_status_perf.py`)

- [ ] Helper to synthesize a fixture task with 100 loops, 100 evidence rows, 5 best states.
- [ ] `test_report_under_200ms` — invoke `hungerloop report` 5 times, assert median < 200 ms.
- [ ] `test_status_under_200ms` — same, for `hungerloop status`.
- [ ] Mark perf tests with `@pytest.mark.perf` and exclude from default `pytest tests/` run via `pyproject.toml` markers; opt-in via `pytest tests/perf/ -m perf`.

### Tests (`test_trace_export.py`)

- [ ] `test_export_emits_one_line_per_event`
- [ ] `test_export_chronological_order`
- [ ] `test_export_empty_task_emits_no_lines_exits_zero`
- [ ] `test_export_unknown_task_exits_1`
- [ ] `test_export_each_line_is_valid_json`
- [ ] `test_export_event_type_field_uses_enum_value` — assert string matches `EventType` member values from `b0-05`.

## Done when

- [ ] All 6 functional tests pass.
- [ ] Perf tests pass on a developer laptop (consider relaxing to 500 ms in CI).
- [ ] `mypy --strict` clean.
- [ ] PRD §22.8 references this implementation.

## Notes

- JSONL is intentionally minimal — no headers, no envelope, easy to `grep`, easy to pipe to `jq`.
- The 200 ms budget is for warm-cache reads. Cold-start (process spawn + DB open + lock acquire) is excluded; that path is dominated by interpreter startup, not query time.
- Future formats (`prometheus`, `otlp`) layer on top; this command is the canonical event projection.
