"""Unit tests for CostGuard service."""
from unittest.mock import MagicMock

import pytest

from hungerloop.services.cost_guard import CostGuard, SafetyStopError


def _mock_repo(
    cost_usd: float = 0.0,
    tokens: int = 0,
    max_cost: float = 10.0,
    max_tokens: int = 1_000_000,
) -> MagicMock:
    repo = MagicMock()

    clock = MagicMock()
    clock.consumed_by_cost_usd = cost_usd
    clock.consumed_tokens = tokens
    repo.get_hunger_clock.return_value = clock

    policy = MagicMock()
    policy.max_total_cost_usd = max_cost
    policy.max_total_tokens = max_tokens
    repo.get_hunger_policy.return_value = policy

    repo.save_hunger_clock = MagicMock()

    return repo


def test_within_budget_no_error() -> None:
    repo = _mock_repo(cost_usd=5.0, max_cost=10.0)
    guard = CostGuard(repo)
    guard.assert_within_budget("t1")


def test_cost_exceeded_raises() -> None:
    repo = _mock_repo(cost_usd=10.0, max_cost=10.0)
    guard = CostGuard(repo)
    with pytest.raises(SafetyStopError, match="Cost ceiling"):
        guard.assert_within_budget("t1")


def test_token_exceeded_raises() -> None:
    repo = _mock_repo(tokens=1_000_000, max_tokens=1_000_000)
    guard = CostGuard(repo)
    with pytest.raises(SafetyStopError, match="Token ceiling"):
        guard.assert_within_budget("t1")


def test_record_llm_usage_updates_and_checks() -> None:
    repo = _mock_repo(cost_usd=9.5, max_cost=10.0)
    guard = CostGuard(repo)

    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cost_usd = 0.6

    with pytest.raises(SafetyStopError):
        guard.record_llm_usage("t1", usage)

    clock = repo.get_hunger_clock.return_value
    assert clock.consumed_tokens == 150
    assert clock.consumed_by_cost_usd == pytest.approx(10.1)


def test_record_tool_cost_updates_and_checks() -> None:
    repo = _mock_repo(tokens=999_990, max_tokens=1_000_000)
    guard = CostGuard(repo)

    with pytest.raises(SafetyStopError):
        guard.record_tool_cost("t1", cost_usd=0.0, tokens=20)
