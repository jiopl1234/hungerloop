# HungerLoop Real Benchmark

This folder contains opt-in benchmarks that run the real HungerLoop CLI against
a real model endpoint and a real SQLite workspace. They are intentionally not
part of `pytest`: network latency, model behavior, and provider rate limits make
them unsuitable for deterministic CI.

## Quick Run

Create a model config that points at your OpenAI-compatible endpoint:

```yaml
provider: openai
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY
base_url: https://api.openai.com/v1
timeout_seconds: 60
temperature: 0.1
```

You can also start from [`model.example.yaml`](./model.example.yaml).

Then run:

```bash
.venv/bin/python benchmarks/real_hungerloop_benchmark.py \
  --model-config path/to/model.yaml \
  --output-dir benchmark-runs
```

For unknown/self-hosted compatible model names, pass:

```bash
.venv/bin/python benchmarks/real_hungerloop_benchmark.py \
  --model-config path/to/model.yaml \
  --accept-unknown-pricing
```

## What It Measures

The benchmark creates fresh tasks in an isolated temporary workspace and drives
them through the production `hungerloop new`, `hungerloop run`,
`hungerloop status`, `hungerloop report`, and `hungerloop trace export`
commands.

It reports:

- end-to-end task success and stop reason
- wall-clock latency for `new`, `run`, `status`, `report`, and `trace export`
- loop count, committed/rejected loop count, and accepted checks
- model call count, token usage, estimated cost, and model event counts
- tool call count and failed tool call count
- SQLite read latency for status/report/trace surfaces
- event invariants such as model/tool started vs terminal event balance

## Output

Each run writes:

- `real_benchmark_report.json`
- `real_benchmark_report.md`
- one subdirectory per scenario containing the workspace, SQLite DB, acceptance
  file, generated model config, and raw command outputs

The script never writes API keys into generated files. It only stores the
`api_key_env` variable name from your model config.

## Troubleshooting

- `provider_http_error:405` usually means `base_url` is not the OpenAI
  compatible API root expected by HungerLoop. The client posts to
  `<base_url>/chat/completions`; for most gateways `base_url` should end in
  `/v1`.
- `auth_error: openai credentials invalid or unauthorized` means the endpoint
  accepted the route but rejected the key or model authorization. Confirm that
  the environment variable named by `api_key_env` is exported in the same shell
  that runs the benchmark.
- `invalid_provider_json_response` or `provider_json_not_object` means the
  gateway did not return a Chat Completions JSON object.
