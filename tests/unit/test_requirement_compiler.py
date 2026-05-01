import pytest

from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus
from hungerloop.services.requirement_compiler import RuleBasedCompiler


def test_requires_core_acceptance_checks() -> None:
    compiler = RuleBasedCompiler()
    with pytest.raises(ValueError, match="core_acceptance_checks"):
        compiler.compile("t1", "Build a report", hints={})


def test_creates_h001_and_h002() -> None:
    compiler = RuleBasedCompiler()
    goal, ledger = compiler.compile(
        "t1",
        "Build a report",
        hints={
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "report.md"},
                    "description": "Report exists",
                }
            ],
        },
    )

    assert len(ledger.items) == 2
    assert ledger.items[0].id == "H-001"
    assert ledger.items[1].id == "H-002"
    assert ledger.items[1].acceptance_checks[0].check_type == AcceptanceCheckType.EVIDENCE_COUNT_MIN


def test_memory_consolidation_disabled_by_default() -> None:
    compiler = RuleBasedCompiler()
    _, ledger = compiler.compile(
        "t1",
        "Build a report",
        hints={
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "report.md"},
                    "description": "Report exists",
                }
            ],
        },
    )

    item_ids = {item.id for item in ledger.items}
    assert "H-003" not in item_ids


def test_memory_consolidation_enabled_explicitly() -> None:
    compiler = RuleBasedCompiler()
    _, ledger = compiler.compile(
        "t1",
        "Build a report",
        hints={
            "core_acceptance_checks": [
                {
                    "check_type": "file_exists",
                    "params": {"path": "report.md"},
                    "description": "Report exists",
                }
            ],
            "enable_memory_consolidation": True,
        },
    )

    item_ids = {item.id for item in ledger.items}
    assert "H-003" in item_ids
    h003 = [i for i in ledger.items if i.id == "H-003"][0]
    assert h003.acceptance_checks[0].check_type == AcceptanceCheckType.HUMAN_APPROVAL
