"""ModelConfig + ModelConfigLoader + PricingTable (PRD §10, §11.3).

YAML config rules (PRD §10.1):

* Plaintext API keys in config files are forbidden.
* ``provider: openai`` requires ``api_key_env`` *and* the named env var must
  be set before the loader returns.
* ``provider: azure_openai`` is rejected at runtime in v0.5a (Azure ships
  in v0.5b+).

PricingTable returns USD/token estimates; unknown models log an
``unknown_model_pricing`` event and return 0.0 so local tests don't crash
on novel model names.
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.model_client import ModelAuthError


class ModelProvider(str, Enum):
    """Backends recognised by :class:`ModelConfigLoader` (PRD §10.2)."""

    DUMMY = "dummy"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"


class ModelConfig(BaseModel):
    """Immutable model configuration loaded from YAML (PRD §10.2)."""

    provider: ModelProvider = ModelProvider.DUMMY
    model_name: str = "dummy"
    api_key_env: str | None = None
    base_url: str | None = None
    timeout_seconds: int = Field(default=60, ge=1)
    max_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.1, ge=0.0)

    azure_endpoint: str | None = None
    azure_deployment: str | None = None
    azure_api_version: str | None = None


class ModelConfigLoader:
    """Parse ``model_config.yaml`` with the §10.3 safety rules."""

    def load(self, path: Path) -> ModelConfig:
        """Read ``path``, validate, and return a :class:`ModelConfig`.

        Raises:
            ValueError: plaintext ``api_key:`` or missing ``api_key_env``.
            NotImplementedError: ``provider: azure_openai`` (v0.5a).
            ModelAuthError: ``api_key_env`` set but the env var is not.
        """
        raw_text = path.read_text(encoding="utf-8")
        raw: Any = yaml.safe_load(raw_text)
        if not isinstance(raw, dict):
            raise ValueError(
                f"model config must be a YAML mapping, got {type(raw).__name__}"
            )
        if "api_key" in raw:
            raise ValueError(
                "Do not put literal API keys in config. Use api_key_env."
            )
        config = ModelConfig(**raw)

        if config.provider == ModelProvider.AZURE_OPENAI:
            raise NotImplementedError(
                "Azure runtime ships in v0.5b+; use openai or dummy in v0.5a"
            )

        if config.provider == ModelProvider.OPENAI:
            if not config.api_key_env:
                raise ValueError("openai provider requires api_key_env")
            if not os.getenv(config.api_key_env):
                raise ModelAuthError(
                    f"Environment variable {config.api_key_env} is not set"
                )

        return config


class PricingTable:
    """Per-token pricing for known OpenAI models (PRD §11.3).

    Prices are USD per 1M tokens; values are illustrative defaults that
    operators should review before production use. Unknown models return
    0.0 *and* emit an ``unknown_model_pricing`` event so tests / demos
    remain usable but the gap is observable.
    """

    PRICES: dict[str, dict[str, float]] = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 5.00, "output": 15.00},
        "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00},
    }

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def estimate(
        self, model: str, *, input_tokens: int, output_tokens: int
    ) -> float:
        """Return cost in USD for the given token split."""
        if model not in self.PRICES:
            self.repo.append_event(
                "unknown_model_pricing",
                {
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
            return 0.0
        price = self.PRICES[model]
        return (
            input_tokens / 1_000_000 * price["input"]
            + output_tokens / 1_000_000 * price["output"]
        )
