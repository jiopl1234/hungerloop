"""Unit tests for AgentSpecRegistry (PRD §6 / §28.8)."""
from __future__ import annotations

import pytest

from hungerloop.models.worker import AgentSpec
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.agent_registry import (
    EXECUTION_WORKER_V1,
    EXECUTION_WORKER_V1_ID,
    AgentSpecRegistry,
)


def test_default_registry_contains_execution_worker_v1() -> None:
    registry = AgentSpecRegistry()
    spec = registry.get_agent_spec(EXECUTION_WORKER_V1_ID)
    assert spec.agent_id == "execution_worker_v1"
    assert spec.kind == "execution"
    assert spec.allowed_tools == ["read_file", "write_file", "patch_file", "run_shell"]


def test_default_spec_has_expected_output_schema() -> None:
    assert EXECUTION_WORKER_V1.output_schema_name == "ExecutionWorkerResult"
    assert EXECUTION_WORKER_V1.name == "ExecutionWorkerV1"


def test_get_unknown_agent_raises_keyerror() -> None:
    registry = AgentSpecRegistry()
    with pytest.raises(KeyError, match="not registered"):
        registry.get_agent_spec("nonexistent_agent")


def test_list_specs_returns_all_registered() -> None:
    registry = AgentSpecRegistry()
    specs = registry.list_specs()
    assert len(specs) == 1
    assert specs[0].agent_id == EXECUTION_WORKER_V1_ID


def test_register_defaults_seeds_repo() -> None:
    repo = InMemoryRepository()
    registry = AgentSpecRegistry()
    registry.register_defaults(repo)
    seeded = repo.get_agent_spec(EXECUTION_WORKER_V1_ID)
    assert seeded.name == "ExecutionWorkerV1"


def test_register_defaults_is_idempotent() -> None:
    repo = InMemoryRepository()
    registry = AgentSpecRegistry()
    registry.register_defaults(repo)
    registry.register_defaults(repo)
    assert repo.get_agent_spec(EXECUTION_WORKER_V1_ID).agent_id == EXECUTION_WORKER_V1_ID


def test_custom_specs_override_defaults() -> None:
    custom = AgentSpec(agent_id="custom_worker", name="Custom", kind="research")
    registry = AgentSpecRegistry({"custom_worker": custom})
    assert registry.get_agent_spec("custom_worker").kind == "research"
    with pytest.raises(KeyError):
        registry.get_agent_spec(EXECUTION_WORKER_V1_ID)


def test_constructor_copies_input_dict() -> None:
    custom = AgentSpec(agent_id="x", name="X")
    seed = {"x": custom}
    registry = AgentSpecRegistry(seed)
    seed.clear()
    assert registry.get_agent_spec("x").agent_id == "x"
