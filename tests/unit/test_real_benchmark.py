"""Unit tests for the real benchmark helper script."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "real_hungerloop_benchmark.py"
    )
    spec = importlib.util.spec_from_file_location(
        "real_hungerloop_benchmark", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_safe_model_config_drops_unrelated_fields() -> None:
    module = _load_module()
    config = {
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "timeout_seconds": 60,
        "temperature": 0.1,
        "secret_inline": "should-not-survive",
    }

    safe = module._safe_model_config(config)

    assert safe == {
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "timeout_seconds": 60,
        "temperature": 0.1,
    }


def test_build_summary_aggregates_usage_and_success() -> None:
    module = _load_module()
    results = [
        {
            "name": "a",
            "success": True,
            "commands": {"run": {"elapsed_ms": 1200.0}},
            "analysis": {
                "usage": {
                    "tokens": 100,
                    "cost_usd": 0.01,
                    "llm_calls": 1,
                    "tool_calls": 2,
                }
            },
        },
        {
            "name": "b",
            "success": False,
            "commands": {"run": {"elapsed_ms": 1800.0}},
            "analysis": {
                "usage": {
                    "tokens": 300,
                    "cost_usd": 0.02,
                    "llm_calls": 2,
                    "tool_calls": 1,
                }
            },
        },
    ]

    summary = module._build_summary(
        results,
        {
            "provider": "openai",
            "model_name": "gpt-4o-mini",
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
            "temperature": 0.1,
        },
        Path("/tmp/bench"),
    )

    assert summary["overall"]["scenario_count"] == 2
    assert summary["overall"]["passed_scenarios"] == 1
    assert summary["overall"]["failed_scenarios"] == 1
    assert summary["overall"]["success_rate"] == 0.5
    assert summary["overall"]["total_tokens"] == 400
    assert summary["overall"]["total_cost_usd"] == 0.03
    assert summary["overall"]["total_llm_calls"] == 3
    assert summary["overall"]["total_tool_calls"] == 3
