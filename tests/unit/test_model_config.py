"""Unit tests for ModelConfig / ModelConfigLoader / PricingTable (PRD §10-11)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import ModelAuthError
from hungerloop.services.model_config import (
    ModelConfig,
    ModelConfigLoader,
    ModelProvider,
    PricingTable,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "model.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loader_rejects_literal_api_key(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "provider: openai\nmodel_name: gpt-4o\napi_key: sk-secret\n",
    )
    with pytest.raises(ValueError, match="Do not put literal API keys"):
        ModelConfigLoader().load(p)


def test_loader_rejects_literal_api_key_for_dummy_too(tmp_path: Path) -> None:
    """Security regression net (PRD §6.4): the api_key check is provider-
    independent; a YAML that wires ``provider: dummy`` AND ``api_key:`` is
    still rejected so a future copy-paste can't smuggle a key past the
    dummy branch.
    """
    p = _write(
        tmp_path,
        "provider: dummy\nmodel_name: dummy\napi_key: sk-leak\n",
    )
    with pytest.raises(ValueError, match="Do not put literal API keys"):
        ModelConfigLoader().load(p)


def test_loader_rejects_azure_provider(tmp_path: Path) -> None:
    p = _write(tmp_path, "provider: azure_openai\nmodel_name: gpt-4o\n")
    with pytest.raises(NotImplementedError, match="Azure runtime"):
        ModelConfigLoader().load(p)


def test_loader_requires_api_key_env_for_openai(tmp_path: Path) -> None:
    p = _write(tmp_path, "provider: openai\nmodel_name: gpt-4o\n")
    with pytest.raises(ValueError, match="api_key_env"):
        ModelConfigLoader().load(p)


def test_loader_requires_env_var_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    p = _write(
        tmp_path,
        "provider: openai\nmodel_name: gpt-4o\napi_key_env: MISSING_KEY\n",
    )
    with pytest.raises(ModelAuthError, match="MISSING_KEY"):
        ModelConfigLoader().load(p)


def test_loader_loads_openai_with_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_OPENAI_KEY", "sk-test")
    p = _write(
        tmp_path,
        (
            "provider: openai\nmodel_name: gpt-4o-mini\n"
            "api_key_env: MY_OPENAI_KEY\nbase_url: https://api.openai.com/v1\n"
        ),
    )
    config = ModelConfigLoader().load(p)
    assert config.provider is ModelProvider.OPENAI
    assert config.model_name == "gpt-4o-mini"
    assert config.api_key_env == "MY_OPENAI_KEY"


def test_loader_loads_native_tool_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_OPENAI_KEY", "sk-test")
    p = _write(
        tmp_path,
        (
            "provider: openai\nmodel_name: gpt-4o-mini\n"
            "api_key_env: MY_OPENAI_KEY\ntool_protocol: native\n"
        ),
    )
    config = ModelConfigLoader().load(p)
    assert config.tool_protocol == "native"


def test_loader_loads_openai_passthrough_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_OPENAI_KEY", "sk-test")
    p = _write(
        tmp_path,
        (
            "provider: openai\n"
            "model_name: gpt-4o-mini\n"
            "api_key_env: MY_OPENAI_KEY\n"
            "reasoning_effort: medium\n"
            "extra_headers:\n"
            "  OpenAI-Beta: responses=v1\n"
            "extra_args:\n"
            "  response_format:\n"
            "    type: json_object\n"
            "  metadata:\n"
            "    mission_id: M-1\n"
        ),
    )

    config = ModelConfigLoader().load(p)

    assert config.reasoning_effort == "medium"
    assert config.extra_headers == {"OpenAI-Beta": "responses=v1"}
    assert config.extra_args == {
        "response_format": {"type": "json_object"},
        "metadata": {"mission_id": "M-1"},
    }


def test_loader_dummy_skips_env_check(tmp_path: Path) -> None:
    p = _write(tmp_path, "provider: dummy\nmodel_name: dummy\n")
    config = ModelConfigLoader().load(p)
    assert config.provider is ModelProvider.DUMMY


def test_loader_rejects_non_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path, "- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="YAML mapping"):
        ModelConfigLoader().load(p)


def test_pricing_known_model_estimates() -> None:
    repo = InMemoryRepository()
    table = PricingTable(repo)
    cost = table.estimate("gpt-4o-mini", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.15)


def test_pricing_unknown_model_emits_event() -> None:
    repo = InMemoryRepository()
    table = PricingTable(repo)
    cost = table.estimate("phantom-model", input_tokens=100, output_tokens=200)
    assert cost == 0.0
    assert any(e["event_type"] == "unknown_model_pricing" for e in repo._events)


def test_pricing_combines_input_and_output() -> None:
    repo = InMemoryRepository()
    table = PricingTable(repo)
    cost = table.estimate(
        "gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == pytest.approx(20.0)


def test_model_config_defaults() -> None:
    cfg = ModelConfig()
    assert cfg.provider is ModelProvider.DUMMY
    assert cfg.timeout_seconds == 60
    assert cfg.max_tokens == 4096
    assert cfg.tool_protocol == "json_actions"
    assert cfg.reasoning_effort is None
    assert cfg.extra_args == {}
    assert cfg.extra_headers == {}
