# b1-02 · DummyModelClient long-term contract

**Spec**: §8. **PRD**: §6.4. **Release**: v0.5b.1.

## Goal

Make `provider: dummy` a first-class ModelConfig option with a documented long-term contract: sanctioned for tests, demos, CI smoke. Warning-only on YAML load; silent on test injection.

## Files to touch

- `src/hungerloop/services/model_config.py` — accept `provider: dummy`; skip api_key_env requirement.
- `src/hungerloop/cli/run_cmd.py` — emit warning when YAML-loaded config has `provider: dummy` (unless `HUNGERLOOP_QUIET_DUMMY=1`).
- `src/hungerloop/services/model_client.py` — add a frozen-API comment block on `DummyModelClient.with_actions` action schema.
- `tests/unit/test_model_config.py` (extend) — `provider: dummy` round-trip.
- **NEW** `tests/unit/test_dummy_warning.py`.

## Checklist

### ModelConfig loader (`model_config.py`)

- [ ] Add `"dummy"` to the recognized provider set.
- [ ] When `provider == "dummy"`: do NOT require `api_key_env`; do NOT require `model_name` (default to `"dummy"`).
- [ ] Existing `"openai"` and `"azure_openai"` paths unchanged.

### CLI warning (`run_cmd.py`)

- [ ] After loading the model config from YAML, if `config.provider == "dummy"`:
  - If `os.environ.get("HUNGERLOOP_QUIET_DUMMY") in {"1", "true", "yes"}`: silent.
  - Else: `click.echo("Warning: using DummyModelClient — outputs are scripted and not from a real model.", err=True)`.
- [ ] **Crucially**: this code path is only the YAML loader. When tests inject a model client via `CliContext(model_client=DummyModelClient(...))`, `run_cmd.py` never re-loads YAML — so the warning never fires. Verify this in the test below.

### Action schema freeze (`model_client.py`)

- [ ] Add a comment block above `DummyModelClient.with_actions`:

```python
# ----- FROZEN ACTION SCHEMA (v0.5b.1+) -----
# Each action is a dict with at least:
#   tool_name: str   - matches a Tool registered in tool_harness
#   args: dict       - tool-specific args (any JSON-serializable shape)
# Optional keys:
#   delay_ms: int    - test scaffolding for timing assertions
# Adding new optional keys is allowed across minor releases.
# Removing or renaming any of the above requires a major-version bump
# on this schema and a coordinated test migration.
# -------------------------------------------
```

## Tests

### `test_model_config.py` (extend)

- [ ] `test_loader_accepts_provider_dummy_without_api_key_env`
- [ ] `test_loader_still_rejects_literal_api_key_for_dummy_too` — security regression net.

### `test_dummy_warning.py` (new)

- [ ] `test_yaml_dummy_emits_stderr_warning` — synthesize a YAML with `provider: dummy`, run `hungerloop run`, assert stderr contains "DummyModelClient".
- [ ] `test_yaml_dummy_silenced_by_env_var` — set `HUNGERLOOP_QUIET_DUMMY=1`, no warning.
- [ ] `test_test_injection_path_is_silent` — `runner.invoke(cli, [...], obj=CliContext(model_client=DummyModelClient(...)))` — assert no warning in stderr.

## Done when

- [ ] All 5 tests pass (2 in extend + 3 new).
- [ ] `mypy --strict` clean.
- [ ] PRD §6.4 references this implementation.
- [ ] `examples/demo_task.yaml` already uses `provider: dummy` — confirm it still loads; document in the demo's README that the warning is expected.

## Notes

- Refusal would force every CI smoke run to thread `--allow-dummy` through `Makefile`/`pytest` wrappers. The friction outweighs the safety; the warning is enough.
- `HUNGERLOOP_QUIET_DUMMY=1` is the pragma for CI logs. Document it in `RELEASE_CHECKLIST.md` if the test runner pipes stderr.
