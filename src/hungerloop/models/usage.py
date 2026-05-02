"""Usage models for HungerLoop v0.5a.

:class:`UsageSnapshot` is the running cost/token/call counter for a task,
read by :class:`~hungerloop.services.budget_guard.BudgetGuard` and the
Orchestrator's loop-trace builder (PRD §16.4).

:class:`ModelUsage` is the per-call payload reported by ``ModelClient``
to ``CostGuard.record_llm_usage`` (PRD §10.2). Promoted into Day 2 so
``CostGuard`` can be typed without depending on the not-yet-shipped
ModelClient package.
"""
from __future__ import annotations

from pydantic import BaseModel


class UsageSnapshot(BaseModel):
    """Cumulative resource consumption for a task."""

    task_id: str
    tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0


class ModelUsage(BaseModel):
    """Per-call usage payload reported by ModelClient (PRD §10.2)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
