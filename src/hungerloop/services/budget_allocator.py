"""Per-loop :class:`BudgetAllocation` builder (PRD §4.1, §12.2 step 8).

The Orchestrator calls :meth:`BudgetAllocator.allocate` once per loop with
the current :class:`HungerSnapshot`. The default rules in v0.5a are deliberately
flat — phase-aware rather than learned — so behaviour is reproducible and
easy to read in a :class:`LoopTrace`.

Phase semantics:

* **EXPLORE** — full token / tool budget, full wall-clock; encourage the
  worker to attempt new acceptance checks.
* **EXPLOIT** — same token budget, shorter wall-clock; the agent should
  be converging, not branching.
* **COOLDOWN** — token / tool budgets cut hard so most loops settle into
  no-progress and the global stagnation streak escalates to ``BLOCKED``.

Every dimension is a kwarg on the constructor so demo / test code can
swap in tighter values without subclassing.
"""
from __future__ import annotations

from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerSnapshot
from hungerloop.models.planning import BudgetAllocation


class BudgetAllocator:
    """Map :class:`HungerSnapshot.phase` to a :class:`BudgetAllocation`."""

    def __init__(
        self,
        *,
        explore_max_tokens: int = 4000,
        explore_max_tool_calls: int = 10,
        explore_max_wall_clock_seconds: int = 300,
        exploit_max_tokens: int = 4000,
        exploit_max_tool_calls: int = 8,
        exploit_max_wall_clock_seconds: int = 180,
        cooldown_max_tokens: int = 500,
        cooldown_max_tool_calls: int = 1,
        cooldown_max_wall_clock_seconds: int = 60,
        max_workers_per_loop: int = 1,
        max_model_retries: int = 2,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 20.0,
        allow_shell: bool = True,
        allow_file_write: bool = True,
        allow_network: bool = False,
    ) -> None:
        self._phase_caps: dict[LoopPhase, tuple[int, int, int]] = {
            LoopPhase.EXPLORE: (
                explore_max_tokens,
                explore_max_tool_calls,
                explore_max_wall_clock_seconds,
            ),
            LoopPhase.EXPLOIT: (
                exploit_max_tokens,
                exploit_max_tool_calls,
                exploit_max_wall_clock_seconds,
            ),
            LoopPhase.COOLDOWN: (
                cooldown_max_tokens,
                cooldown_max_tool_calls,
                cooldown_max_wall_clock_seconds,
            ),
        }
        self._max_workers_per_loop = max_workers_per_loop
        self._max_model_retries = max_model_retries
        self._retry_base = retry_base_delay_seconds
        self._retry_max = retry_max_delay_seconds
        self._allow_shell = allow_shell
        self._allow_file_write = allow_file_write
        self._allow_network = allow_network

    def allocate(self, snapshot: HungerSnapshot) -> BudgetAllocation:
        """Build a :class:`BudgetAllocation` for the snapshot's phase."""
        max_tokens, max_tool_calls, max_wall_clock = self._phase_caps[snapshot.phase]
        return BudgetAllocation(
            phase=snapshot.phase,
            max_tokens=max_tokens,
            max_tool_calls=max_tool_calls,
            max_wall_clock_seconds=max_wall_clock,
            max_workers_per_loop=self._max_workers_per_loop,
            max_model_retries=self._max_model_retries,
            retry_base_delay_seconds=self._retry_base,
            retry_max_delay_seconds=self._retry_max,
            allow_shell=self._allow_shell,
            allow_file_write=self._allow_file_write,
            allow_network=self._allow_network,
        )
