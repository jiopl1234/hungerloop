"""Planning models for HungerLoop v0.5a.

:class:`LoopPlan` describes which hunger items to work on in a loop iteration.
:class:`BudgetAllocation` is the per-loop / per-worker resource envelope, with
fields covering wall-clock, retry, and side-effect policy gates (PRD §4.2,
§28.11, §28.18).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from hungerloop.models.enums import LoopPhase

WorkerRole = Literal["executor", "validator"]


class Assignment(BaseModel):
    """Agent assignment within a loop plan."""

    assignment_id: str = Field(
        description="Assignment identifier in ASGN-<task_id>-<loop_id>-<index> format."
    )
    agent_id: str
    mission: str
    target_hunger_item_ids: list[str] = Field(default_factory=list)
    target_feature_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    role: WorkerRole = "executor"
    max_retries: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)


class LoopPlan(BaseModel):
    """Plan for one loop iteration."""

    task_id: str
    loop_id: int
    selected_hunger_item_ids: list[str] = Field(default_factory=list)
    assignments: list[Assignment] = Field(
        default_factory=list,
        description="Assignments in topological order; dependencies appear before dependents.",
    )
    phase: LoopPhase = LoopPhase.EXPLORE
    rationale: str = ""


class BudgetAllocation(BaseModel):
    """Per-phase / per-worker budget envelope.

    The envelope spans three orthogonal dimensions:

    1. **Resources** — ``max_tokens``, ``max_tool_calls``, ``max_wall_clock_seconds``.
       Enforced by :class:`~hungerloop.services.budget_guard.BudgetGuard` (M12 / ADR-002).
    2. **Concurrency** — ``max_workers_per_loop`` (planner-side; v0.5a fixed to 1).
    3. **Retry policy** — ``max_assignment_retries``, ``max_model_retries``,
       ``retry_base_delay_seconds``, ``retry_max_delay_seconds``. Assignment
       retries are consumed by the M3 worker scheduler; model retries are
       consumed by ``ModelClient.complete_json`` (M6 / ADR-004);
       ``retry_max_delay_seconds`` is also the ceiling for ``Retry-After``-driven
       sleeps.
    4. **Side-effect policy** — ``allow_shell``, ``allow_file_write``,
       ``allow_network``. Enforced by :class:`ToolHarness` (PRD §28.11).

    Pydantic ``model_validator`` rejects retry max < base.
    """

    phase: LoopPhase

    # Resources
    # NOTE: max_tokens is a per-loop CUMULATIVE budget across all model
    # calls the worker makes (including inner self-repair iterations from
    # the A-patched ExecutionWorker). 4000 was the v0.4.1 default when the
    # worker was strictly single-call; with MAX_SELF_REPAIR_ITERATIONS=5
    # and reasoning-heavy models the worker can comfortably need 50K+
    # cumulative tokens per loop.
    max_tokens: int = Field(default=60000, ge=0)
    # max_tool_calls similarly needs headroom for inner-loop (write + N
    # run_shell verifies + edits all count). 10 was too tight on harder
    # tasks like mini-sql (41 verification_steps imply many run_shell).
    max_tool_calls: int = Field(default=80, ge=0)
    max_wall_clock_seconds: int = Field(default=300, ge=1)

    # Concurrency
    max_workers_per_loop: int = Field(default=1, ge=1)
    max_new_items_per_loop: int = Field(default=3, ge=0)
    scrutiny_timeout_seconds: int = Field(default=120, ge=1)

    # Retry policy (read by ModelClient.complete_json)
    max_assignment_retries: int = Field(default=1, ge=0)
    max_model_retries: int = Field(default=4, ge=0)
    retry_base_delay_seconds: float = Field(default=1.0, ge=0.0)
    retry_max_delay_seconds: float = Field(default=20.0, ge=0.0)

    # Side-effect policy gates (enforced by ToolHarness)
    allow_shell: bool = True
    allow_file_write: bool = True
    allow_network: bool = False

    @model_validator(mode="after")
    def _validate_retry_window(self) -> BudgetAllocation:
        """Reject retry_max_delay_seconds < retry_base_delay_seconds."""
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError(
                f"retry_max_delay_seconds ({self.retry_max_delay_seconds}) "
                f"must be >= retry_base_delay_seconds ({self.retry_base_delay_seconds})"
            )
        return self
