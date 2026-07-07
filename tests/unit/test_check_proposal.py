"""Unit tests for the CheckProposal model (VAL-SYN-001, VAL-SYN-002)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal

# ---------------------------------------------------------------------------
# VAL-SYN-001: Check proposals accept only deterministic check types
# ---------------------------------------------------------------------------


def test_shell_exit_zero_proposal_succeeds() -> None:
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": ["python", "-m", "pytest", "-q"]},
        description="Run pytest",
        source_quote="The project must pass pytest",
        proposed_by="synthesizer",
    )
    assert proposal.check_type == AcceptanceCheckType.SHELL_EXIT_ZERO
    assert proposal.params["argv"] == ["python", "-m", "pytest", "-q"]


def test_file_exists_proposal_succeeds() -> None:
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": "src/hungerloop/models/synthesis.py"},
        description="Synthesis module exists",
        source_quote="The synthesis module must exist",
        proposed_by="synthesizer",
    )
    assert proposal.check_type == AcceptanceCheckType.FILE_EXISTS
    assert proposal.params["path"] == "src/hungerloop/models/synthesis.py"


def test_llm_judge_proposal_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.LLM_JUDGE,
            params={"prompt": "Is the code good?"},
            description="LLM judge",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_evidence_count_min_proposal_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.EVIDENCE_COUNT_MIN,
            params={"min_count": 1},
            description="Evidence count",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_human_approval_proposal_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.HUMAN_APPROVAL,
            params={},
            description="Human approval",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_artifact_type_exists_proposal_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.ARTIFACT_TYPE_EXISTS,
            params={"artifact_type": "report"},
            description="Artifact exists",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_empty_source_quote_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": ["python", "-m", "pytest"]},
            description="Run pytest",
            source_quote="",
            proposed_by="synthesizer",
        )


def test_whitespace_only_source_quote_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": ["python", "-m", "pytest"]},
            description="Run pytest",
            source_quote="   ",
            proposed_by="synthesizer",
        )


def test_missing_source_quote_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": ["python", "-m", "pytest"]},
            description="Run pytest",
            proposed_by="synthesizer",
        )


def test_non_dict_params_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params="not a dict",  # type: ignore[arg-type]
            description="Run pytest",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_missing_argv_for_shell_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"description": "no argv here"},
            description="Run pytest",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_missing_path_for_file_exists_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"description": "no path here"},
            description="File exists",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_argv_not_list_for_shell_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": "python -m pytest"},
            description="Run pytest",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_argv_empty_list_for_shell_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
            params={"argv": []},
            description="Run pytest",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_path_not_string_for_file_exists_fails() -> None:
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": 123},
            description="File exists",
            source_quote="Some quote",
            proposed_by="synthesizer",
        )


def test_proposed_by_defaults_to_unknown() -> None:
    """proposed_by should have a sensible default."""
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": "report.md"},
        description="File exists",
        source_quote="Some quote",
    )
    assert proposal.proposed_by is not None


def test_proposal_serialization_round_trip() -> None:
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": ["python", "-m", "pytest", "-q"]},
        description="Run pytest",
        source_quote="The project must pass pytest",
        proposed_by="synthesizer",
    )
    raw = proposal.model_dump_json()
    restored = CheckProposal.model_validate_json(raw)
    assert restored.check_type == proposal.check_type
    assert restored.params == proposal.params
    assert restored.description == proposal.description
    assert restored.source_quote == proposal.source_quote
    assert restored.proposed_by == proposal.proposed_by


# ---------------------------------------------------------------------------
# VAL-SYN-002: Proposal deduplication keys are stable for behavior
# ---------------------------------------------------------------------------


def _shell_proposal(argv: list[str], description: str = "desc") -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv},
        description=description,
        source_quote="Some quote",
        proposed_by="synthesizer",
    )


def _file_proposal(path: str, description: str = "desc") -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=description,
        source_quote="Some quote",
        proposed_by="synthesizer",
    )


def test_dedup_shell_executable_normalized_python3() -> None:
    """python3 and python produce the same dedup key."""
    p1 = _shell_proposal(["python3", "-m", "pytest", "-q"])
    p2 = _shell_proposal(["python", "-m", "pytest", "-q"])
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_shell_executable_normalized_python_exe() -> None:
    """python.exe and python produce the same dedup key."""
    p1 = _shell_proposal(["python.exe", "-m", "pytest", "-q"])
    p2 = _shell_proposal(["python", "-m", "pytest", "-q"])
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_shell_executable_normalized_python311() -> None:
    """python3.11 and python produce the same dedup key."""
    p1 = _shell_proposal(["python3.11", "-m", "pytest", "-q"])
    p2 = _shell_proposal(["python", "-m", "pytest", "-q"])
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_shell_argv_whitespace_normalized() -> None:
    """Leading/trailing whitespace in argv elements is stripped."""
    p1 = _shell_proposal(["python", "-m", "pytest", "-q"])
    p2 = _shell_proposal([" python ", " -m ", " pytest ", " -q "])
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_file_path_backslash_normalized() -> None:
    """Backslash and forward slash paths produce the same dedup key."""
    p1 = _file_proposal("src/hungerloop/models/synthesis.py")
    p2 = _file_proposal("src\\hungerloop\\models\\synthesis.py")
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_file_path_dot_prefix_normalized() -> None:
    """Leading ./ is stripped in dedup key."""
    p1 = _file_proposal("src/hungerloop/models/synthesis.py")
    p2 = _file_proposal("./src/hungerloop/models/synthesis.py")
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_file_path_double_slash_normalized() -> None:
    """Double slashes are collapsed in dedup key."""
    p1 = _file_proposal("src/hungerloop/models/synthesis.py")
    p2 = _file_proposal("src//hungerloop/models/synthesis.py")
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_description_ignored() -> None:
    """Different descriptions produce the same dedup key."""
    p1 = _shell_proposal(["python", "-m", "pytest"], description="Run pytest")
    p2 = _shell_proposal(["python", "-m", "pytest"], description="Run tests")
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_file_description_ignored() -> None:
    """Different descriptions for file proposals produce the same dedup key."""
    p1 = _file_proposal("report.md", description="Report file")
    p2 = _file_proposal("report.md", description="Output file")
    assert p1.dedup_key() == p2.dedup_key()


def test_dedup_different_check_types_different_keys() -> None:
    """Shell and file proposals produce different dedup keys."""
    p_shell = _shell_proposal(["python", "-m", "pytest"])
    p_file = _file_proposal("python")
    assert p_shell.dedup_key() != p_file.dedup_key()


def test_dedup_different_commands_different_keys() -> None:
    """Different shell commands produce different dedup keys."""
    p1 = _shell_proposal(["python", "-m", "pytest"])
    p2 = _shell_proposal(["python", "-m", "ruff"])
    assert p1.dedup_key() != p2.dedup_key()


def test_dedup_different_paths_different_keys() -> None:
    """Different file paths produce different dedup keys."""
    p1 = _file_proposal("src/hungerloop/models/synthesis.py")
    p2 = _file_proposal("src/hungerloop/models/hunger.py")
    assert p1.dedup_key() != p2.dedup_key()


def test_dedup_semantic_args_case_sensitive() -> None:
    """Argument values with different case produce different dedup keys."""
    p1 = _shell_proposal(["python", "-m", "pytest", "-q"])
    p2 = _shell_proposal(["python", "-m", "pytest", "-Q"])
    assert p1.dedup_key() != p2.dedup_key()


def test_dedup_different_args_different_keys() -> None:
    """Different number of arguments produces different dedup keys."""
    p1 = _shell_proposal(["python", "-m", "pytest", "-q"])
    p2 = _shell_proposal(["python", "-m", "pytest"])
    assert p1.dedup_key() != p2.dedup_key()


def test_dedup_key_is_string() -> None:
    proposal = _shell_proposal(["python", "-m", "pytest"])
    assert isinstance(proposal.dedup_key(), str)


def test_dedup_key_starts_with_check_type() -> None:
    """Dedup key includes the check type for cross-type inequality."""
    shell_key = _shell_proposal(["python", "-m", "pytest"]).dedup_key()
    file_key = _file_proposal("report.md").dedup_key()
    assert "shell_exit_zero" in shell_key
    assert "file_exists" in file_key
