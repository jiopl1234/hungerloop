"""Unit tests for v0.6 planning model extensions."""
from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from hungerloop.models.planning import Assignment, LoopPlan, WorkerRole
from hungerloop.services.mission_planner import PlannerCycleError
from hungerloop.services.mission_planner import WorkerRole as PlannerWorkerRole


def test_assignment_exposes_m3_fields_with_defaults() -> None:
    assignment = Assignment(
        assignment_id="ASGN-task-1-0",
        agent_id="execution_worker_v1",
        target_hunger_item_ids=[],
        allowed_tools=[],
        mission="",
    )

    assert "assignment_id" in Assignment.model_fields
    assert "target_feature_ids" in Assignment.model_fields
    assert "depends_on" in Assignment.model_fields
    assert "role" in Assignment.model_fields
    assert "max_retries" in Assignment.model_fields
    assert "retry_count" in Assignment.model_fields
    assert assignment.assignment_id == "ASGN-task-1-0"
    assert assignment.target_feature_ids == []
    assert assignment.depends_on == []
    assert assignment.role == "executor"
    assert assignment.max_retries == 0
    assert assignment.retry_count == 0


@pytest.mark.parametrize("role", ["executor", "validator"])
def test_assignment_accepts_supported_worker_roles(role: WorkerRole) -> None:
    assignment = Assignment(
        assignment_id=f"ASGN-task-1-{role}",
        agent_id="execution_worker_v1",
        target_hunger_item_ids=[],
        allowed_tools=[],
        mission="",
        role=role,
    )

    assert assignment.role == role


def test_assignment_rejects_unknown_worker_role() -> None:
    with pytest.raises(ValidationError):
        Assignment(
            assignment_id="ASGN-task-1-0",
            agent_id="execution_worker_v1",
            target_hunger_item_ids=[],
            allowed_tools=[],
            mission="",
            role="other",  # type: ignore[arg-type]
        )


def test_worker_role_alias_matches_m3_contract() -> None:
    assert get_args(WorkerRole) == ("executor", "validator")
    assert PlannerWorkerRole is WorkerRole


def test_planner_cycle_error_is_value_error() -> None:
    assert issubclass(PlannerCycleError, ValueError)


def test_loop_plan_documents_topological_assignment_order() -> None:
    field = LoopPlan.model_fields["assignments"]

    assert field.description is not None
    assert "topological order" in field.description
