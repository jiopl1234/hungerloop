#!/usr/bin/env python
"""Run real HungerLoop end-to-end benchmarks against a configured model.

This benchmark intentionally uses the production CLI and a real SQLite-backed
workspace. It is opt-in because it performs network model calls and can spend
provider quota.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if _SRC_ROOT.exists():
    sys.path.insert(0, str(_SRC_ROOT))


@dataclass(frozen=True)
class Scenario:
    name: str
    goal: str
    acceptance: dict[str, object]
    max_loops: int


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="file_write_basic",
        goal=(
            "Create report.md. The file must mention HungerLoop, benchmark, "
            "tokens, latency, and workspace isolation. Use only the allowed "
            "tools and return JSON actions."
        ),
        acceptance={
            "core_acceptance_mode": "all",
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "report.md"},
                    "description": "report.md exists.",
                },
                {
                    "check_type": "shell_exit_zero",
                    "params": {
                        "argv": [
                            "python",
                            "-c",
                            (
                                "from pathlib import Path\n"
                                "text = Path('report.md').read_text()\n"
                                "required = ['HungerLoop', 'benchmark', "
                                "'tokens', 'latency', 'workspace isolation']\n"
                                "missing = [word for word in required if word not in text]\n"
                                "raise SystemExit(0 if not missing else 1)\n"
                            ),
                        ],
                        "timeout": 10,
                    },
                    "description": "report.md contains required benchmark terms.",
                },
            ],
        },
        max_loops=4,
    ),
    Scenario(
        name="json_artifact",
        goal=(
            "Create metrics.json with valid JSON containing keys "
            "scenario, success_criteria, and risks. success_criteria must be "
            "a list with at least three strings. Use only JSON actions."
        ),
        acceptance={
            "core_acceptance_mode": "all",
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "metrics.json"},
                    "description": "metrics.json exists.",
                },
                {
                    "check_type": "shell_exit_zero",
                    "params": {
                        "argv": [
                            "python",
                            "-c",
                            (
                                "import json\n"
                                "from pathlib import Path\n"
                                "data = json.loads(Path('metrics.json').read_text())\n"
                                "ok = isinstance(data.get('success_criteria'), list) "
                                "and len(data['success_criteria']) >= 3 "
                                "and 'scenario' in data and 'risks' in data\n"
                                "raise SystemExit(0 if ok else 1)\n"
                            ),
                        ],
                        "timeout": 10,
                    },
                    "description": "metrics.json has the required shape.",
                },
            ],
        },
        max_loops=4,
    ),
    Scenario(
        name="shell_validation",
        goal=(
            "Create script.py that prints exactly hungerloop-ok when executed. "
            "The implementation should be minimal and deterministic. Use only "
            "JSON actions."
        ),
        acceptance={
            "core_acceptance_mode": "all",
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "script.py"},
                    "description": "script.py exists.",
                },
                {
                    "check_type": "shell_exit_zero",
                    "params": {
                        "argv": [
                            "python",
                            "-c",
                            (
                                "import subprocess, sys\n"
                                "out = subprocess.check_output("
                                "[sys.executable, 'script.py'], text=True).strip()\n"
                                "raise SystemExit(0 if out == 'hungerloop-ok' else 1)\n"
                            ),
                        ],
                        "timeout": 10,
                    },
                    "description": "script.py prints hungerloop-ok.",
                },
            ],
        },
        max_loops=4,
    ),
)


def main() -> int:
    args = _parse_args()
    model_config = _load_model_config(args.model_config)
    _validate_model_env(model_config)

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"real-benchmark-{run_id}"
    run_dir.mkdir()

    script = Path(__file__).resolve()
    repo_root = script.parents[1]
    command_base = _command_base(repo_root)

    results: list[dict[str, object]] = []
    for scenario in SCENARIOS:
        if args.scenario and scenario.name not in args.scenario:
            continue
        results.append(
            _run_scenario(
                scenario,
                run_dir,
                model_config,
                args.model_config,
                command_base,
                accept_unknown_pricing=args.accept_unknown_pricing,
                command_timeout_seconds=args.command_timeout_seconds,
            )
        )

    summary = _build_summary(results, model_config, run_dir)
    json_path = run_dir / "real_benchmark_report.json"
    md_path = run_dir / "real_benchmark_report.md"
    json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    md_path.write_text(_format_markdown(summary), encoding="utf-8")

    overall = summary["overall"]
    assert isinstance(overall, dict)
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")
    print(f"Overall success: {float(overall['success_rate']):.1%}")
    return 0 if int(overall["failed_scenarios"]) == 0 else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-config",
        type=Path,
        required=True,
        help="OpenAI-compatible HungerLoop model config YAML.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark-runs"),
        help="Directory where timestamped benchmark output is written.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[scenario.name for scenario in SCENARIOS],
        help="Scenario to run. Repeat to run a subset. Defaults to all.",
    )
    parser.add_argument(
        "--accept-unknown-pricing",
        action="store_true",
        help="Pass through to hungerloop run for custom compatible models.",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=180,
        help="Timeout for each CLI command.",
    )
    return parser.parse_args()


def _load_model_config(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"model config must be a mapping: {path}")
    if "api_key" in raw:
        raise SystemExit("model config must not contain literal api_key")
    provider = raw.get("provider")
    if provider != "openai":
        raise SystemExit("real benchmark requires provider: openai")
    return raw


def _validate_model_env(config: dict[str, object]) -> None:
    api_key_env = config.get("api_key_env")
    if not isinstance(api_key_env, str) or not api_key_env:
        raise SystemExit("model config requires api_key_env")
    if not os.getenv(api_key_env):
        raise SystemExit(f"environment variable is not set: {api_key_env}")


def _command_base(repo_root: Path) -> list[str]:
    local_cli = repo_root / ".venv" / "bin" / "hungerloop"
    if local_cli.exists():
        return [str(local_cli)]
    found = shutil.which("hungerloop")
    if found:
        return [found]
    return [sys.executable, "-c", "from hungerloop.cli.main import cli; cli()"]


def _run_scenario(
    scenario: Scenario,
    run_dir: Path,
    model_config: dict[str, object],
    source_model_config: Path,
    command_base: list[str],
    *,
    accept_unknown_pricing: bool,
    command_timeout_seconds: int,
) -> dict[str, object]:
    scenario_dir = run_dir / scenario.name
    scenario_dir.mkdir()
    acceptance_path = scenario_dir / "acceptance.yaml"
    task_id = f"bench-{scenario.name}"
    acceptance_path.write_text(
        yaml.safe_dump(scenario.acceptance, sort_keys=False),
        encoding="utf-8",
    )

    generated_config = scenario_dir / "model.yaml"
    generated_config.write_text(
        yaml.safe_dump(_safe_model_config(model_config), sort_keys=False),
        encoding="utf-8",
    )

    commands: dict[str, dict[str, object]] = {}
    commands["new"] = _run_command(
        command_base
        + [
            "new",
            scenario.goal,
            "--task-id",
            task_id,
            "--accept-file",
            str(acceptance_path),
        ],
        cwd=scenario_dir,
        timeout=command_timeout_seconds,
    )

    run_args = command_base + [
        "run",
        task_id,
        "--max-loops",
        str(scenario.max_loops),
        "--model-config",
        str(generated_config),
    ]
    if accept_unknown_pricing:
        run_args.append("--accept-unknown-pricing")
    commands["run"] = _run_command(
        run_args,
        cwd=scenario_dir,
        timeout=command_timeout_seconds,
    )
    commands["status"] = _run_command(
        command_base + ["status", task_id],
        cwd=scenario_dir,
        timeout=command_timeout_seconds,
    )
    commands["report"] = _run_command(
        command_base + ["report", task_id, "--format", "json"],
        cwd=scenario_dir,
        timeout=command_timeout_seconds,
    )
    commands["trace_export"] = _run_command(
        command_base + ["trace", "export", task_id, "--format", "jsonl"],
        cwd=scenario_dir,
        timeout=command_timeout_seconds,
    )

    for name, record in commands.items():
        (scenario_dir / f"{name}.stdout.txt").write_text(
            str(record["stdout"]),
            encoding="utf-8",
        )
        (scenario_dir / f"{name}.stderr.txt").write_text(
            str(record["stderr"]),
            encoding="utf-8",
        )

    db_path = scenario_dir / "hungerloop.sqlite"
    analysis = _analyze_repository(db_path, task_id)
    surface_perf = _measure_read_surfaces(
        command_base,
        scenario_dir,
        task_id,
        command_timeout_seconds,
    )

    report_payload: dict[str, Any] = {}
    report_stdout = commands["report"]["stdout"]
    if commands["report"]["returncode"] == 0 and isinstance(report_stdout, str):
        try:
            report_payload = json.loads(report_stdout)
        except json.JSONDecodeError:
            report_payload = {}

    success = (
        commands["run"]["returncode"] == 0
        and report_payload.get("stop", {}).get("reason") == "done"
        and analysis["event_invariants"]["balanced_tool_calls"]
        and analysis["event_invariants"]["balanced_model_calls"]
    )

    return {
        "name": scenario.name,
        "task_id": task_id,
        "workspace": str(scenario_dir),
        "source_model_config": str(source_model_config),
        "success": success,
        "commands": commands,
        "report": report_payload,
        "analysis": analysis,
        "surface_perf": surface_perf,
    }


def _safe_model_config(config: dict[str, object]) -> dict[str, object]:
    allowed = {
        "provider",
        "model_name",
        "api_key_env",
        "base_url",
        "timeout_seconds",
        "max_tokens",
        "temperature",
    }
    return {key: value for key, value in config.items() if key in allowed}


def _run_command(args: list[str], *, cwd: Path, timeout: int) -> dict[str, object]:
    start = time.perf_counter()
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {
        "args": _redact_args(args),
        "returncode": completed.returncode,
        "elapsed_ms": round(elapsed_ms, 2),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _redact_args(args: list[str]) -> list[str]:
    return [arg if len(arg) < 160 else f"{arg[:157]}..." for arg in args]


def _analyze_repository(db_path: Path, task_id: str) -> dict[str, object]:
    from hungerloop.repository.sqlite_repo import SQLiteRepository

    repo = SQLiteRepository.open(db_path, write_capable=False)
    traces = repo.list_loop_traces(task_id)
    usage = repo.get_usage_snapshot(task_id)
    events = repo.list_events(task_id)
    event_counts: dict[str, int] = {}
    for event in events:
        event_type = str(event["event_type"])
        event_counts[event_type] = event_counts.get(event_type, 0) + 1

    evidence_rows = repo.conn.execute(
        """
        SELECT evidence_type, payload_json
        FROM evidence
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchall()
    tool_elapsed_ms: list[int] = []
    failed_tool_calls = 0
    model_tokens: list[int] = []
    model_costs: list[float] = []
    model_error_rows = 0
    last_model_error: str | None = None
    for row in evidence_rows:
        payload = json.loads(str(row["payload_json"]))
        if row["evidence_type"] == "tool_call":
            tool_elapsed_ms.append(int(payload.get("elapsed_ms", 0)))
            if not bool(payload.get("success", False)):
                failed_tool_calls += 1
        if row["evidence_type"] == "model_call":
            model_tokens.append(
                int(payload.get("input_tokens", 0)) + int(payload.get("output_tokens", 0))
            )
            model_costs.append(float(payload.get("cost_usd", 0.0)))
        if row["evidence_type"] == "model_error":
            model_error_rows += 1
            last_model_error = str(payload.get("error_message", ""))

    stop_report = repo.get_last_stop_report(task_id)
    best = repo.get_best_state(task_id)

    model_started = event_counts.get("model_call_started", 0)
    model_terminal = (
        event_counts.get("model_call_succeeded", 0)
        + event_counts.get("model_call_failed", 0)
        + event_counts.get("model_auth_required", 0)
    )
    tool_started = event_counts.get("tool_call_started", 0)
    tool_terminal = event_counts.get("tool_call_succeeded", 0) + event_counts.get(
        "tool_call_failed", 0
    )

    return {
        "stop_reason": stop_report.stop_reason.value if stop_report else None,
        "goal_status": stop_report.goal_status if stop_report else None,
        "loop_count": len(traces),
        "committed_loops": sum(1 for trace in traces if trace.committed),
        "rejected_loops": sum(1 for trace in traces if not trace.committed),
        "accepted_check_keys": list(best.accepted_check_keys) if best else [],
        "usage": {
            "tokens": usage.tokens,
            "cost_usd": round(usage.cost_usd, 6),
            "llm_calls": usage.llm_calls,
            "tool_calls": usage.tool_calls,
        },
        "evidence": {
            "rows": len(evidence_rows),
            "model_call_rows": len(model_tokens),
            "model_error_rows": model_error_rows,
            "last_model_error": last_model_error,
            "tool_call_rows": len(tool_elapsed_ms),
            "failed_tool_calls": failed_tool_calls,
            "tool_elapsed_ms_p50": _median(tool_elapsed_ms),
            "tool_elapsed_ms_max": max(tool_elapsed_ms) if tool_elapsed_ms else 0,
            "model_tokens_p50": _median(model_tokens),
            "model_cost_total": round(sum(model_costs), 6),
        },
        "events": event_counts,
        "event_invariants": {
            "model_started": model_started,
            "model_terminal": model_terminal,
            "balanced_model_calls": model_started == model_terminal,
            "tool_started": tool_started,
            "tool_terminal": tool_terminal,
            "balanced_tool_calls": tool_started == tool_terminal,
            "has_stop_report_created": event_counts.get("stop_report_created", 0) >= 1,
        },
    }


def _measure_read_surfaces(
    command_base: list[str],
    scenario_dir: Path,
    task_id: str,
    timeout: int,
) -> dict[str, object]:
    timings: dict[str, list[float]] = {
        "status": [],
        "report": [],
        "trace_export": [],
    }
    commands = {
        "status": command_base + ["status", task_id],
        "report": command_base + ["report", task_id, "--format", "json"],
        "trace_export": command_base + ["trace", "export", task_id, "--format", "jsonl"],
    }
    for name, args in commands.items():
        for _ in range(3):
            record = _run_command(args, cwd=scenario_dir, timeout=timeout)
            if record["returncode"] == 0:
                timings[name].append(float(record["elapsed_ms"]))
    return {
        name: {
            "samples_ms": [round(value, 2) for value in values],
            "median_ms": round(statistics.median(values), 2) if values else None,
        }
        for name, values in timings.items()
    }


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _build_summary(
    results: list[dict[str, object]],
    model_config: dict[str, object],
    run_dir: Path,
) -> dict[str, object]:
    successes = sum(1 for result in results if result["success"])
    total_tokens = sum(
        int(result["analysis"]["usage"]["tokens"])  # type: ignore[index]
        for result in results
    )
    total_cost = sum(
        float(result["analysis"]["usage"]["cost_usd"])  # type: ignore[index]
        for result in results
    )
    total_llm_calls = sum(
        int(result["analysis"]["usage"]["llm_calls"])  # type: ignore[index]
        for result in results
    )
    total_tool_calls = sum(
        int(result["analysis"]["usage"]["tool_calls"])  # type: ignore[index]
        for result in results
    )
    run_elapsed = [
        float(result["commands"]["run"]["elapsed_ms"])  # type: ignore[index]
        for result in results
    ]
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_dir": str(run_dir),
        "model": {
            "provider": model_config.get("provider"),
            "model_name": model_config.get("model_name"),
            "base_url": model_config.get("base_url"),
            "api_key_env": model_config.get("api_key_env"),
            "temperature": model_config.get("temperature"),
        },
        "overall": {
            "scenario_count": len(results),
            "passed_scenarios": successes,
            "failed_scenarios": len(results) - successes,
            "success_rate": successes / len(results) if results else 0.0,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "total_llm_calls": total_llm_calls,
            "total_tool_calls": total_tool_calls,
            "run_elapsed_ms_p50": round(statistics.median(run_elapsed), 2)
            if run_elapsed
            else None,
            "run_elapsed_ms_max": round(max(run_elapsed), 2) if run_elapsed else None,
        },
        "scenarios": results,
    }


def _format_markdown(summary: dict[str, object]) -> str:
    overall = summary["overall"]
    lines = [
        "# HungerLoop Real Benchmark Report",
        "",
        f"- created_at: `{summary['created_at']}`",
        f"- model: `{summary['model']['model_name']}`",
        f"- success_rate: `{overall['success_rate']:.1%}`",
        f"- scenarios: `{overall['passed_scenarios']}/{overall['scenario_count']}`",
        f"- total_tokens: `{overall['total_tokens']}`",
        f"- total_cost_usd: `{overall['total_cost_usd']}`",
        f"- run_elapsed_ms_p50: `{overall['run_elapsed_ms_p50']}`",
        "",
        "## Scenarios",
        "",
    ]
    for scenario in summary["scenarios"]:
        analysis = scenario["analysis"]
        usage = analysis["usage"]
        evidence = analysis["evidence"]
        invariants = analysis["event_invariants"]
        lines.extend(
            [
                f"### {scenario['name']}",
                "",
                f"- success: `{scenario['success']}`",
                f"- stop_reason: `{analysis['stop_reason']}`",
                f"- loops: `{analysis['loop_count']}` "
                f"(committed `{analysis['committed_loops']}`, "
                f"rejected `{analysis['rejected_loops']}`)",
                f"- accepted_check_keys: `{analysis['accepted_check_keys']}`",
                f"- run_elapsed_ms: `{scenario['commands']['run']['elapsed_ms']}`",
                f"- tokens: `{usage['tokens']}`",
                f"- cost_usd: `{usage['cost_usd']}`",
                f"- llm_calls/tool_calls: `{usage['llm_calls']}/{usage['tool_calls']}`",
                f"- model_error_rows: `{evidence['model_error_rows']}`",
                f"- last_model_error: `{evidence['last_model_error']}`",
                f"- model events balanced: `{invariants['balanced_model_calls']}`",
                f"- tool events balanced: `{invariants['balanced_tool_calls']}`",
                f"- workspace: `{scenario['workspace']}`",
                "",
            ]
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
