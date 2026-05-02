"""AgentSpecRegistry for HungerLoop v0.5a.

v0.5a does not implement a dynamic agent registry (PRD §6.1). Exactly one
:class:`~hungerloop.models.worker.AgentSpec` is hardcoded —
``execution_worker_v1`` — and seeded into a :class:`RepositoryProtocol` at
startup (or read directly via :meth:`AgentSpecRegistry.get_agent_spec`).

Later versions back this object with the ``agent_specs`` SQLite table; the
registry abstraction keeps the call site identical
(``Orchestrator`` calls ``repo.get_agent_spec(...)`` per PRD §6.2).
"""
from __future__ import annotations

from hungerloop.models.worker import AgentSpec
from hungerloop.repository.protocol import RepositoryProtocol

EXECUTION_WORKER_V1_ID = "execution_worker_v1"

EXECUTION_WORKER_V1 = AgentSpec(
    agent_id=EXECUTION_WORKER_V1_ID,
    name="ExecutionWorkerV1",
    kind="execution",
    output_schema_name="ExecutionWorkerResult",
    allowed_tools=["read_file", "write_file", "patch_file", "run_shell"],
)


class AgentSpecRegistry:
    """Static, code-only registry of agent specs available in v0.5a (PRD §6).

    The default constructor seeds the registry with the hardcoded
    :data:`EXECUTION_WORKER_V1`. Tests can pass a custom mapping to override.

    Use :meth:`register_defaults` once at startup to mirror the registry into
    a :class:`RepositoryProtocol` so that the Orchestrator's
    ``repo.get_agent_spec(...)`` call (PRD §6.2) resolves correctly.
    """

    def __init__(self, specs: dict[str, AgentSpec] | None = None) -> None:
        self._specs: dict[str, AgentSpec] = (
            dict(specs) if specs is not None else {EXECUTION_WORKER_V1_ID: EXECUTION_WORKER_V1}
        )

    def get_agent_spec(self, agent_id: str) -> AgentSpec:
        """Return the registered spec for ``agent_id``.

        Raises:
            KeyError: when ``agent_id`` is not registered. Mirrors
                :meth:`InMemoryRepository.get_agent_spec` so callers can treat
                both backends uniformly.
        """
        if agent_id not in self._specs:
            raise KeyError(f"AgentSpec not registered: {agent_id}")
        return self._specs[agent_id]

    def list_specs(self) -> list[AgentSpec]:
        """Return all registered specs (insertion order)."""
        return list(self._specs.values())

    def register_defaults(self, repo: RepositoryProtocol) -> None:
        """Seed ``repo`` with every registered spec (PRD §6.1).

        Idempotent: ``repo.save_agent_spec`` is keyed by ``agent_id``. Call once
        at orchestrator startup so subsequent ``repo.get_agent_spec`` lookups
        succeed.
        """
        for spec in self._specs.values():
            repo.save_agent_spec(spec)
