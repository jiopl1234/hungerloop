"""Mission planning service primitives for v0.6 M3.

This module starts the MissionPlanner surface with shared role typing and the
cycle error used by the full planner implementation (REQ-M3-002).
"""
from __future__ import annotations

from hungerloop.models.planning import WorkerRole

__all__ = ["PlannerCycleError", "WorkerRole"]


class PlannerCycleError(ValueError):
    """Raised when mission assignment dependencies contain a cycle."""
