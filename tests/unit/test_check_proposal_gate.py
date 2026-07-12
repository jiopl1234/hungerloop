"""Unit tests for CheckProposalGate (VAL-SYN-004, VAL-SYN-005)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.check_proposal_gate import (
    CheckProposalGate,
    DryRunResult,
    SandboxDryRunner,
)
from hungerloop.services.sandbox_runner import SandboxRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shell_proposal(
    argv: list[str],
    description: str = "desc",
    fixture_argv: list[str] | None = None,
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv},
        description=description,
        source_quote="Some quote",
        proposed_by="synthesizer",
        fixture_argv=fixture_argv,
    )


def _file_proposal(path: str, description: str = "desc") -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=description,
        source_quote="Some quote",
        proposed_by="synthesizer",
    )


class FakeDryRunner:
    """Fake dry runner that returns pre-configured results."""

    def __init__(self, results: list[bool] | None = None, default: bool = True) -> None:
        self._results = list(results) if results is not None else []
        self._default = default
        self.call_count = 0
        self.calls: list[tuple[list[str], Path | None]] = []

    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        self.call_count += 1
        self.calls.append((list(argv), cwd))
        if self._results:
            return self._results.pop(0)
        return self._default


class NondeterministicDryRunner:
    """Returns alternating True/False to simulate nondeterminism."""

    def __init__(self) -> None:
        self.call_count = 0

    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        self.call_count += 1
        return self.call_count % 2 == 1


# ---------------------------------------------------------------------------
# VAL-SYN-004: Proposal gating rejects duplicates and unsafe shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_duplicates() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    p1 = _file_proposal("report.md")
    p2 = _file_proposal("report.md")
    result = await gate.filter([p1, p2])
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "duplicate"
    assert result.rejected[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_rejects_duplicates_against_existing_keys() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("report.md")
    existing_key = proposal.dedup_key()
    result = await gate.filter([proposal], existing_keys={existing_key})
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_rejects_absolute_path() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("/etc/passwd")
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1
    assert "absolute" in result.rejected[0].reason.lower()


@pytest.mark.asyncio
async def test_rejects_windows_absolute_path() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("C:\\Users\\secret.txt")
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_rejects_path_traversal() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("../etc/passwd")
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1
    assert "traversal" in result.rejected[0].reason.lower() or (
        "unsafe" in result.rejected[0].reason.lower()
    )


@pytest.mark.asyncio
async def test_rejects_nul_in_path() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("report\x00.md")
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_accepts_safe_file_exists_without_dry_run() -> None:
    """A safe file_exists proposal is accepted without invoking a dry runner."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _file_proposal("src/hungerloop/models/synthesis.py")
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    assert len(result.rejected) == 0
    assert dry_runner.call_count == 0


@pytest.mark.asyncio
async def test_accepts_multiple_safe_file_exists() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposals = [
        _file_proposal("report.md"),
        _file_proposal("src/hungerloop/main.py"),
        _file_proposal("docs/readme.md"),
    ]
    result = await gate.filter(proposals)
    assert len(result.accepted) == 3
    assert len(result.rejected) == 0


@pytest.mark.asyncio
async def test_rejects_missing_shell_argv() -> None:
    """Gate defence-in-depth: shell proposal without argv is rejected."""
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    # Use model_construct to bypass model validation (defence-in-depth test)
    proposal = CheckProposal.model_construct(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={},
        description="test",
        source_quote="quote",
        proposed_by="synth",
    )
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_rejects_non_string_argv_elements() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = CheckProposal.model_construct(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": ["python", 123, "pytest"]},
        description="test",
        source_quote="quote",
        proposed_by="synth",
    )
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_rejects_empty_argv_elements() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = CheckProposal.model_construct(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": ["python", "", "pytest"]},
        description="test",
        source_quote="quote",
        proposed_by="synth",
    )
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_command() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _shell_proposal(["rm", "-rf", "/"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1
    assert "allowlist" in result.rejected[0].reason.lower()


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_command_bash() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _shell_proposal(["bash", "-c", "rm -rf /"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_custom_allowlist_accepts_additional_commands() -> None:
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner, allowlist=["python", "ruff"])
    proposal = _shell_proposal(["ruff", "check", "src"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_reject_reason_codes_are_stable() -> None:
    """Reason codes should be stable strings, not random or None."""
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposals = [
        _file_proposal("/absolute/path"),
        _file_proposal("../traversal"),
        _shell_proposal(["rm", "-rf"]),
    ]
    result = await gate.filter(proposals)
    assert len(result.rejected) == 3
    for r in result.rejected:
        assert isinstance(r.reason, str)
        assert len(r.reason) > 0


@pytest.mark.asyncio
async def test_empty_proposals_returns_empty_result() -> None:
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    result = await gate.filter([])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 0


@pytest.mark.asyncio
async def test_gate_does_not_call_llms() -> None:
    """The gate is deterministic and never calls LLMs."""
    # This is a design assertion - the gate has no model client dependency
    gate = CheckProposalGate()
    result = await gate.filter([_file_proposal("report.md")])
    assert len(result.accepted) == 1


# ---------------------------------------------------------------------------
# VAL-SYN-005: Shell proposals require deterministic dry-runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_dry_run_called_exactly_twice() -> None:
    """Shell proposals are dry-run exactly twice."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest", "-q"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_shell_dry_run_uses_candidate_workspace_cwd(tmp_path: Path) -> None:
    """Gate passes the candidate workspace cwd to shell dry-runs."""
    candidate_root = tmp_path / "workspace" / "tasks" / "t1" / "candidates" / "loop_001" / "files"
    candidate_root.mkdir(parents=True)
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest", "-q"])

    result = await gate.filter([proposal], dry_run_cwd=candidate_root)

    assert len(result.accepted) == 1
    assert dry_runner.calls == [
        (["python", "-m", "pytest", "-q"], candidate_root),
        (["python", "-m", "pytest", "-q"], candidate_root),
    ]


@pytest.mark.asyncio
async def test_sandbox_dry_runner_never_mutates_candidate_workspace(
    tmp_path: Path,
) -> None:
    """Production dry-runs execute in a disposable copy of candidate files."""
    candidate_root = tmp_path / "candidate" / "files"
    candidate_root.mkdir(parents=True)
    original = candidate_root / "state.txt"
    original.write_text("original", encoding="utf-8")
    adapter = SandboxDryRunner(SandboxRunner(InMemoryRepository()))

    passed = await adapter.dry_run(
        [
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "Path('state.txt').write_text('mutated', encoding='utf-8'); "
                "Path('created.txt').write_text('created', encoding='utf-8')"
            ),
        ],
        cwd=candidate_root,
    )

    assert passed is True
    assert original.read_text(encoding="utf-8") == "original"
    assert not (candidate_root / "created.txt").exists()
    assert not list(candidate_root.parent.glob(".hungerloop-dry-run-*"))


@pytest.mark.asyncio
async def test_gate_rejects_syntax_error_as_non_executable() -> None:
    class SyntaxFailureRunner:
        def __init__(self) -> None:
            self.call_count = 0

        async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
            raise AssertionError("gate should use detailed dry-run results")

        async def dry_run_detailed(
            self, argv: list[str], cwd: Path | None = None
        ) -> DryRunResult:
            self.call_count += 1
            return DryRunResult(
                passed=False,
                failure_kind="syntax_error",
                stderr_excerpt="SyntaxError: invalid syntax",
            )

    runner = SyntaxFailureRunner()
    result = await CheckProposalGate(dry_runner=runner).filter(
        [_shell_proposal(["python", "-m", "generated_check"])]
    )

    assert result.accepted == []
    assert result.rejected[0].reason == "assertion_not_executable:syntax_error"
    assert runner.call_count == 1


@pytest.mark.asyncio
async def test_plan_time_statically_rejects_invalid_python_inline_check() -> None:
    runner = FakeDryRunner(default=True)
    result = await CheckProposalGate(dry_runner=runner).filter(
        [_shell_proposal(["python", "-c", "try: pass; except: pass"])],
        defer_fixture_precheck=True,
    )

    assert result.accepted == []
    assert result.rejected[0].reason == "assertion_not_executable:syntax_error"
    assert runner.call_count == 0


@pytest.mark.asyncio
async def test_sandbox_dry_runner_classifies_real_python_syntax_error(
    tmp_path: Path,
) -> None:
    result = await SandboxDryRunner(
        SandboxRunner(InMemoryRepository())
    ).dry_run_detailed(
        [sys.executable, "-c", "try: pass; except: pass"],
        cwd=tmp_path,
    )

    assert result.passed is False
    assert result.failure_kind == "syntax_error"
    assert "SyntaxError" in result.stderr_excerpt


@pytest.mark.asyncio
async def test_shell_accepted_when_deterministic_pass() -> None:
    """Both runs pass -> accepted."""
    dry_runner = FakeDryRunner(results=[True, True])
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_shell_accepted_when_deterministic_fail() -> None:
    """Both runs fail -> accepted (deterministic, just currently failing)."""
    dry_runner = FakeDryRunner(results=[False, False])
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_shell_rejected_when_nondeterministic() -> None:
    """Differing outcomes -> rejected as nondeterministic."""
    dry_runner = NondeterministicDryRunner()
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1
    assert "nondeterministic" in result.rejected[0].reason.lower()


@pytest.mark.asyncio
async def test_shell_rejected_when_nondeterministic_fail_pass() -> None:
    """First run fails, second passes -> rejected."""
    dry_runner = FakeDryRunner(results=[False, True])
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_shell_rejected_when_nondeterministic_pass_fail() -> None:
    """First run passes, second fails -> rejected."""
    dry_runner = FakeDryRunner(results=[True, False])
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 0
    assert len(result.rejected) == 1


@pytest.mark.asyncio
async def test_no_raw_shell_bypass() -> None:
    """The gate uses the DryRunner protocol, not raw shell execution."""
    # The gate should accept a DryRunner and use it, not subprocess directly
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python", "-m", "pytest"])
    await gate.filter([proposal])
    # If the gate used raw subprocess, the fake dry_runner would not have been called
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_multiple_shell_proposals_each_dry_run_twice() -> None:
    """Each shell proposal gets exactly two dry runs."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposals = [
        _shell_proposal(["python", "-m", "pytest", "-q"]),
        _shell_proposal(["python", "-m", "ruff", "check", "src"]),
    ]
    result = await gate.filter(proposals)
    assert len(result.accepted) == 2
    assert dry_runner.call_count == 4  # 2 proposals * 2 runs each


@pytest.mark.asyncio
async def test_mixed_proposals() -> None:
    """Mix of file_exists and shell proposals."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposals = [
        _file_proposal("report.md"),
        _shell_proposal(["python", "-m", "pytest"]),
        _file_proposal("src/hungerloop/main.py"),
    ]
    result = await gate.filter(proposals)
    assert len(result.accepted) == 3
    # Only the shell proposal triggers dry runs
    assert dry_runner.call_count == 2


@pytest.mark.asyncio
async def test_dry_run_uses_normalized_argv() -> None:
    """The gate passes the original (non-normalized) argv to the dry runner."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    proposal = _shell_proposal(["python3", "-m", "pytest", "-q"])
    result = await gate.filter([proposal])
    assert len(result.accepted) == 1
    # The dry runner should receive the original argv
    call_argv = dry_runner.calls[0][0]
    assert call_argv == ["python3", "-m", "pytest", "-q"]


@pytest.mark.asyncio
async def test_rejected_proposal_has_dedup_key() -> None:
    """Rejected proposals include their dedup key for auditability."""
    gate = CheckProposalGate(dry_runner=FakeDryRunner(default=True))
    proposal = _file_proposal("/absolute/path")
    result = await gate.filter([proposal])
    assert len(result.rejected) == 1
    assert result.rejected[0].dedup_key == proposal.dedup_key()


@pytest.mark.asyncio
async def test_accepted_proposals_added_to_dedup_history() -> None:
    """Accepted proposals prevent future duplicates within the same batch."""
    dry_runner = FakeDryRunner(default=True)
    gate = CheckProposalGate(dry_runner=dry_runner)
    p1 = _shell_proposal(["python", "-m", "pytest"])
    p2 = _shell_proposal(["python3", "-m", "pytest"])  # Normalizes to same key
    result = await gate.filter([p1, p2])
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1


async def test_required_shell_fixture_must_be_present() -> None:
    runner = FakeDryRunner(default=True)
    result = await CheckProposalGate(dry_runner=runner).filter(
        [_shell_proposal(["python", "-c", "print('check')"])],
        require_fixture=True,
    )

    assert result.accepted == []
    assert result.rejected[0].reason == "missing_fixture"
    assert runner.call_count == 0


async def test_fixture_failure_rejects_before_assertion_dry_run() -> None:
    runner = FakeDryRunner(results=[False, False])
    proposal = _shell_proposal(
        ["python", "-c", "print('assertion')"],
        fixture_argv=["python", "-c", "raise SystemExit(1)"],
    )

    result = await CheckProposalGate(dry_runner=runner).filter(
        [proposal],
        require_fixture=True,
    )

    assert result.accepted == []
    assert result.rejected[0].reason == "fixture_setup_failed"
    assert runner.call_count == 2


async def test_failing_assertion_is_accepted_when_fixture_is_valid() -> None:
    runner = FakeDryRunner(results=[True, True, False, False])
    proposal = _shell_proposal(
        ["python", "-c", "raise SystemExit(1)"],
        fixture_argv=["python", "-c", "print('fixture')"],
    )

    result = await CheckProposalGate(dry_runner=runner).filter(
        [proposal],
        require_fixture=True,
    )

    assert result.accepted == [proposal]
    assert runner.call_count == 4


async def test_plan_time_defers_all_shell_execution() -> None:
    runner = FakeDryRunner(default=False)
    proposal = _shell_proposal(
        ["python", "-c", "raise SystemExit(1)"],
        fixture_argv=["python", "-c", "print('fixture')"],
    )

    result = await CheckProposalGate(dry_runner=runner).filter(
        [proposal],
        require_fixture=True,
        defer_fixture_precheck=True,
    )

    assert result.accepted == [proposal]
    assert runner.call_count == 0
