"""Focused unit tests for RefinementCompiler.compile_spec_coverage.

Covers VAL-SYN-006 (injection with provenance) and VAL-SYN-007
(idempotent deduplication and per-call caps).
"""
from __future__ import annotations

from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
)
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.refinement_compiler import RefinementCompiler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shell_proposal(
    argv: list[str] | None = None,
    *,
    description: str = "run tests",
    source_quote: str = "The project must pass tests.",
    proposed_by: str = "synthesizer",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv or ["python", "-m", "pytest", "-q"]},
        description=description,
        source_quote=source_quote,
        proposed_by=proposed_by,
    )


def _file_proposal(
    path: str = "src/main.py",
    *,
    description: str = "main file exists",
    source_quote: str = "The project must have a main file.",
    proposed_by: str = "synthesizer",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=description,
        source_quote=source_quote,
        proposed_by=proposed_by,
    )


def _seed_ledger(repo: InMemoryRepository, task_id: str, items: list[HungerItem]) -> None:
    repo.save_hunger_ledger(task_id, HungerLedger(task_id=task_id, items=items))


def _get_ledger(repo: InMemoryRepository, task_id: str) -> HungerLedger:
    return repo.get_hunger_ledger(task_id)


# ---------------------------------------------------------------------------
# VAL-SYN-006: Injection with provenance
# ---------------------------------------------------------------------------


class TestCompileSpecCoverageInjection:
    """Tests proving accepted proposals become H-SYN-NNN items with full provenance."""

    def test_accepted_proposal_becomes_h_syn_item(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal()

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        assert result == ["H-SYN-001"]
        ledger = _get_ledger(repo, "t1")
        item = ledger.items[0]
        assert item.id == "H-SYN-001"
        assert item.refinement_kind == "spec_coverage"
        assert item.refinement_tier == 1
        assert item.generated_by == "synthesizer"
        assert item.status == HungerItemStatus.OPEN
        assert item.gap_score == 1.0

    def test_acceptance_check_matches_proposal(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal(
            argv=["python", "-m", "pytest", "-q"],
            description="python pytest suite passes",
        )

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        item = _get_ledger(repo, "t1").items[0]
        assert len(item.acceptance_checks) == 1
        check = item.acceptance_checks[0]
        assert check.check_type == AcceptanceCheckType.SHELL_EXIT_ZERO
        assert check.params["argv"] == ["python", "-m", "pytest", "-q"]
        assert check.description == "python pytest suite passes"

    def test_file_exists_proposal_check_preserved(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _file_proposal(path="src/main.py", description="main file exists")

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        item = _get_ledger(repo, "t1").items[0]
        check = item.acceptance_checks[0]
        assert check.check_type == AcceptanceCheckType.FILE_EXISTS
        assert check.params["path"] == "src/main.py"
        assert check.description == "main file exists"

    def test_tier_is_preserved(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal()

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
            tier=3,
        )

        item = _get_ledger(repo, "t1").items[0]
        assert item.refinement_tier == 3

    def test_default_tier_is_one(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal()

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        item = _get_ledger(repo, "t1").items[0]
        assert item.refinement_tier == 1

    def test_generated_by_worker_provenance(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _file_proposal(proposed_by="worker:agent-1")

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="worker:agent-1",
        )

        item = _get_ledger(repo, "t1").items[0]
        assert item.generated_by == "worker:agent-1"

    def test_nested_proposed_by_does_not_override_generated_by(self) -> None:
        """Worker-controlled proposed_by cannot spoof ledger provenance."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _file_proposal(proposed_by="worker:spoofed")

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="worker:real-agent",
        )

        item = _get_ledger(repo, "t1").items[0]
        assert item.generated_by == "worker:real-agent"

    def test_provenance_source_quote_preserved(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal(source_quote="Spec says tests must pass.")

        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        # source_quote is preserved in auditable metadata (events)
        events = repo.list_events("t1")
        assert len(events) > 0
        # The source_quote appears in at least one event payload
        found = any(
            "Spec says tests must pass." in str(evt.get("payload", {}))
            for evt in events
        )
        assert found

    def test_sequential_ids_for_multiple_proposals(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposals = [
            _shell_proposal(argv=["python", "-m", "pytest"]),
            _file_proposal(path="src/app.py"),
        ]

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=proposals,
            generated_by="synthesizer",
        )

        assert result == ["H-SYN-001", "H-SYN-002"]
        ledger = _get_ledger(repo, "t1")
        assert [item.id for item in ledger.items] == ["H-SYN-001", "H-SYN-002"]

    def test_ids_continue_after_existing_h_syn_items(self) -> None:
        repo = InMemoryRepository()
        existing = HungerItem(
            id="H-SYN-001",
            title="existing syn",
            refinement_kind="spec_coverage",
            refinement_tier=1,
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                    params={"argv": ["python", "-m", "pytest", "-x"]},
                    description="existing",
                )
            ],
        )
        _seed_ledger(repo, "t1", [existing])

        proposal = _file_proposal(path="src/new.py")
        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        assert result == ["H-SYN-002"]

    def test_preserves_existing_ledger_items(self) -> None:
        repo = InMemoryRepository()
        base = HungerItem(id="H-001", title="base deliverable")
        _seed_ledger(repo, "t1", [base])

        proposal = _file_proposal()
        RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        ledger = _get_ledger(repo, "t1")
        assert [item.id for item in ledger.items] == ["H-001", "H-SYN-001"]


# ---------------------------------------------------------------------------
# VAL-SYN-007: Idempotent deduplication and caps
# ---------------------------------------------------------------------------


class TestCompileSpecCoverageDedup:
    """Tests proving duplicate proposals and existing equivalent checks are skipped."""

    def test_duplicate_proposal_in_same_call_skipped(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal(argv=["python", "-m", "pytest", "-q"])

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal, proposal.model_copy()],
            generated_by="synthesizer",
        )

        assert result == ["H-SYN-001"]
        ledger = _get_ledger(repo, "t1")
        assert len(ledger.items) == 1

    def test_duplicate_proposal_across_calls_skipped(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal(argv=["python", "-m", "pytest", "-q"])

        compiler = RefinementCompiler(repo)
        first = compiler.compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )
        second = compiler.compile_spec_coverage(
            task_id="t1",
            proposals=[proposal.model_copy()],
            generated_by="synthesizer",
        )

        assert first == ["H-SYN-001"]
        assert second == []

    def test_equivalent_proposal_different_description_skipped(self) -> None:
        """Proposals with same dedup key (different description) are duplicates."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        p1 = _shell_proposal(argv=["python", "-m", "pytest"], description="desc 1")
        p2 = _shell_proposal(argv=["python", "-m", "pytest"], description="desc 2")

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[p1, p2],
            generated_by="synthesizer",
        )

        assert result == ["H-SYN-001"]

    def test_existing_equivalent_check_in_ledger_skipped(self) -> None:
        """If the ledger already has an equivalent acceptance check, skip."""
        repo = InMemoryRepository()
        existing = HungerItem(
            id="H-001",
            title="base",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                    params={"argv": ["python", "-m", "pytest", "-q"]},
                    description="tests pass",
                )
            ],
        )
        _seed_ledger(repo, "t1", [existing])

        proposal = _shell_proposal(argv=["python", "-m", "pytest", "-q"])
        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        assert result == []

    def test_existing_file_exists_check_in_ledger_skipped(self) -> None:
        repo = InMemoryRepository()
        existing = HungerItem(
            id="H-001",
            title="base",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/main.py"},
                    description="main file",
                )
            ],
        )
        _seed_ledger(repo, "t1", [existing])

        proposal = _file_proposal(path="src/main.py")
        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        assert result == []

    def test_different_proposals_both_injected(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        p1 = _shell_proposal(argv=["python", "-m", "pytest"])
        p2 = _file_proposal(path="src/app.py")

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[p1, p2],
            generated_by="synthesizer",
        )

        assert len(result) == 2

    def test_normalized_executable_dedup(self) -> None:
        """python3 and python are normalized to the same dedup key."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        p1 = _shell_proposal(argv=["python3", "-m", "pytest", "-q"])
        p2 = _shell_proposal(argv=["python", "-m", "pytest", "-q"])

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[p1, p2],
            generated_by="synthesizer",
        )

        assert len(result) == 1

    def test_normalized_path_dedup(self) -> None:
        """Paths with different separators are normalized for dedup."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        p1 = _file_proposal(path="src\\main.py")
        p2 = _file_proposal(path="src/main.py")

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[p1, p2],
            generated_by="synthesizer",
        )

        assert len(result) == 1


class TestCompileSpecCoverageCaps:
    """Tests proving per-call caps including zero are honored."""

    def test_max_new_items_zero_injects_nothing(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposal = _shell_proposal()

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
            max_new_items=0,
        )

        assert result == []
        ledger = _get_ledger(repo, "t1")
        assert len(ledger.items) == 0

    def test_max_new_items_one_caps_at_one(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])
        proposals = [
            _shell_proposal(argv=["python", "-m", "pytest"]),
            _file_proposal(path="src/app.py"),
        ]

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=proposals,
            generated_by="synthesizer",
            max_new_items=1,
        )

        assert len(result) == 1
        assert result[0] == "H-SYN-001"

    def test_no_save_when_no_new_items(self) -> None:
        """No repository save occurs when no proposal is new."""
        repo = InMemoryRepository()
        existing = HungerItem(
            id="H-001",
            title="base",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                    params={"argv": ["python", "-m", "pytest", "-q"]},
                    description="tests pass",
                )
            ],
        )
        _seed_ledger(repo, "t1", [existing])

        # Record the original ledger object identity; if no save occurs,
        # the repository still holds the same object reference.
        original_ledger = repo._ledgers["t1"]  # type: ignore[attr-defined]

        proposal = _shell_proposal(argv=["python", "-m", "pytest", "-q"])
        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
        )

        assert result == []
        # Ledger unchanged: same items, no new H-SYN items
        ledger = _get_ledger(repo, "t1")
        assert len(ledger.items) == 1
        assert ledger.items[0].id == "H-001"
        # The repository still holds the same ledger reference (no save)
        assert repo._ledgers["t1"] is original_ledger  # type: ignore[attr-defined]

    def test_no_save_when_max_new_items_zero(self) -> None:
        """No repository save occurs when max_new_items=0."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])

        proposal = _shell_proposal()
        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[proposal],
            generated_by="synthesizer",
            max_new_items=0,
        )

        assert result == []
        ledger = _get_ledger(repo, "t1")
        assert len(ledger.items) == 0

    def test_empty_proposals_returns_empty_no_save(self) -> None:
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=[],
            generated_by="synthesizer",
        )

        assert result == []
        ledger = _get_ledger(repo, "t1")
        assert len(ledger.items) == 0

    def test_default_max_new_items_is_20(self) -> None:
        """The default max_new_items is 20."""
        repo = InMemoryRepository()
        _seed_ledger(repo, "t1", [])

        proposals = [
            _file_proposal(path=f"src/file_{i}.py") for i in range(25)
        ]

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=proposals,
            generated_by="synthesizer",
        )

        assert len(result) == 20

    def test_mixed_accepted_and_skipped_respects_cap(self) -> None:
        repo = InMemoryRepository()
        existing = HungerItem(
            id="H-001",
            title="base",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/existing.py"},
                    description="existing",
                )
            ],
        )
        _seed_ledger(repo, "t1", [existing])

        proposals = [
            _file_proposal(path="src/existing.py"),  # duplicate, skipped
            _file_proposal(path="src/new1.py"),
            _file_proposal(path="src/new2.py"),
        ]

        result = RefinementCompiler(repo).compile_spec_coverage(
            task_id="t1",
            proposals=proposals,
            generated_by="synthesizer",
            max_new_items=1,
        )

        assert result == ["H-SYN-001"]
