"""Documentation synchronization guard for ADR-010 and CLAUDE.md.

Covers VAL-REF-025 (refactor transaction documentation artifacts are
synchronized) and VAL-CROSS-013 (formal v0.7 spec path is synchronized).

This test reads the actual documentation files and asserts that required
refactor-transaction documentation keywords are present, that strict I-3
is preserved by default, and that ADR-010 is named as the only approved
I-3 amendment. It fails if either document drifts toward score-based
commits or default regression tolerance.

It also verifies that the formal v0.7 spec path exists and reflects
delivered scope, and that only the affected placeholder specs are
updated with accurate delivered-scope links.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ADR_010_PATH = (
    _REPO_ROOT / "docs" / "architecture" / "v0.7" / "adr"
    / "ADR-010-refactor-transactions.md"
)
_CLAUDE_MD_PATH = _REPO_ROOT / "CLAUDE.md"
_SPEC_PATH = (
    _REPO_ROOT
    / "specs"
    / "v0.7_implementation"
    / "2026-07-07-loop-objective-evolution-design.md"
)

# Placeholder spec paths.
_LLM_PLANNER_PLACEHOLDER = (
    _REPO_ROOT / "specs" / "v0.7_placeholders" / "llm_planner.md"
)
_FAN_OUT_PLACEHOLDER = (
    _REPO_ROOT / "specs" / "v0.7_placeholders" / "concurrent_fan_out_and_join.md"
)
_MEMORY_RECALL_PLACEHOLDER = (
    _REPO_ROOT / "specs" / "v0.7_placeholders" / "cross_task_memory_recall.md"
)
_SERVICES_YAML_PLACEHOLDER = (
    _REPO_ROOT / "specs" / "v0.7_placeholders" / "services_yaml_rich_semantics.md"
)
_WEB_UI_PLACEHOLDER = (
    _REPO_ROOT / "specs" / "v0.7_placeholders" / "web_ui.md"
)


# ---------------------------------------------------------------------------
# VAL-REF-025: ADR-010 documentation guard
# ---------------------------------------------------------------------------

class TestADR010DocumentationSync:
    """Guard for ADR-010 refactor transaction documentation.

    Asserts that ADR-010 documents:
    - enabled-policy gate (refactor_transactions_enabled)
    - declared-regression tolerance (only declared keys tolerated)
    - deadline-bounded behavior (derived deadline, cannot be extended)
    - rollback-on-failure rules
    - strict I-3 default preserved
    - ADR-010 as the only approved I-3 amendment
    - No score-based commit language
    - No default regression tolerance language
    """

    @pytest.fixture
    def adr_text(self) -> str:
        assert _ADR_010_PATH.exists(), (
            f"ADR-010 not found at {_ADR_010_PATH}"
        )
        return _ADR_010_PATH.read_text(encoding="utf-8")

    def test_adr_010_exists(self, adr_text: str) -> None:
        assert len(adr_text) > 0, "ADR-010 is empty"

    def test_adr_010_documents_enabled_policy_gate(self, adr_text: str) -> None:
        """ADR-010 must document enabled-policy gate."""
        lower = adr_text.lower()
        assert "refactor_transactions_enabled" in lower, (
            "ADR-010 must reference refactor_transactions_enabled policy flag"
        )
        assert "false" in lower, (
            "ADR-010 must state the default is disabled (False)"
        )

    def test_adr_010_documents_declared_regression(self, adr_text: str) -> None:
        """ADR-010 must document declared-regression tolerance."""
        lower = adr_text.lower()
        assert "declared" in lower and "regression" in lower, (
            "ADR-010 must document declared regression keys"
        )
        assert "declared_regression_keys" in lower, (
            "ADR-010 must reference declared_regression_keys"
        )

    def test_adr_010_documents_deadline_bounded(self, adr_text: str) -> None:
        """ADR-010 must document deadline-bounded behavior."""
        lower = adr_text.lower()
        assert "deadline" in lower, (
            "ADR-010 must document deadline-bounded behavior"
        )
        assert "deadline_loop" in lower or "refactor_deadline_loops" in lower, (
            "ADR-010 must reference deadline_loop or refactor_deadline_loops"
        )
        assert (
            "cannot supply" in lower
            or "cannot be supplied" in lower
            or "cannot be extended" in lower
            or "cannot supply or extend" in lower
        ), (
            "ADR-010 must state that workers cannot supply or extend deadlines"
        )

    def test_adr_010_documents_rollback_on_failure(self, adr_text: str) -> None:
        """ADR-010 must document rollback-on-failure rules."""
        lower = adr_text.lower()
        assert "rollback" in lower or "roll back" in lower, (
            "ADR-010 must document rollback behavior"
        )
        assert "rolled_back" in lower, (
            "ADR-010 must reference the rolled_back status"
        )

    def test_adr_010_preserves_strict_i3_default(self, adr_text: str) -> None:
        """ADR-010 must preserve strict I-3 by default."""
        lower = adr_text.lower()
        assert "i-3" in lower or "i3" in lower, (
            "ADR-010 must reference invariant I-3"
        )
        assert "strict" in lower, (
            "ADR-010 must mention strict I-3 behavior"
        )
        assert "default" in lower, (
            "ADR-010 must clarify I-3 is strict by default"
        )

    def test_adr_010_is_only_approved_i3_amendment(self, adr_text: str) -> None:
        """ADR-010 must be stated as the only approved I-3 amendment."""
        lower = adr_text.lower()
        assert "only" in lower and "amendment" in lower, (
            "ADR-010 must state it is the only approved I-3 amendment"
        )

    def test_adr_010_no_score_based_commits(self, adr_text: str) -> None:
        """ADR-010 must not enable score-based commits."""
        lower = adr_text.lower()
        # Score-free commit selection should be documented.
        assert "score" in lower, (
            "ADR-010 must address score (to clarify it is excluded)"
        )
        # Must not endorse score-based commit logic.
        forbidden = [
            "score-based commit",
            "score-based promotion",
            "score to decide",
            "score determines",
        ]
        for phrase in forbidden:
            assert phrase not in lower, (
                f"ADR-010 must not contain '{phrase}' (enables score-based commits)"
            )

    def test_adr_010_no_default_regression_tolerance(self, adr_text: str) -> None:
        """ADR-010 must not enable default regression tolerance."""
        lower = adr_text.lower()
        forbidden = [
            "regressions are tolerated by default",
            "default regression tolerance",
            "regressions allowed by default",
            "tolerate regressions by default",
        ]
        for phrase in forbidden:
            assert phrase not in lower, (
                f"ADR-010 must not contain '{phrase}' (default regression tolerance)"
            )


# ---------------------------------------------------------------------------
# VAL-REF-025: CLAUDE.md I-3 amendment guard
# ---------------------------------------------------------------------------

class TestClaudeMdI3AmendmentSync:
    """Guard for CLAUDE.md I-3 amendment.

    Asserts that CLAUDE.md:
    - Preserves strict I-3 by default
    - Names ADR-010 as the only approved I-3 amendment
    - Documents the enabled-policy, declared-regression, deadline-bounded,
      rollback-on-failure rules
    - Does not drift toward score-based commits
    - Does not drift toward default regression tolerance
    """

    @pytest.fixture
    def claude_text(self) -> str:
        assert _CLAUDE_MD_PATH.exists(), (
            f"CLAUDE.md not found at {_CLAUDE_MD_PATH}"
        )
        return _CLAUDE_MD_PATH.read_text(encoding="utf-8")

    def test_claude_md_exists(self, claude_text: str) -> None:
        assert len(claude_text) > 0, "CLAUDE.md is empty"

    def test_claude_md_preserves_strict_i3_default(self, claude_text: str) -> None:
        """CLAUDE.md must preserve strict I-3 by default."""
        lower = claude_text.lower()
        assert "i-3" in lower, "CLAUDE.md must reference I-3"
        assert "score" in lower, (
            "CLAUDE.md must address score (to clarify it is excluded)"
        )
        assert "forbidden" in lower or "schema only" in lower, (
            "CLAUDE.md must state score-based commits are forbidden/schema-only"
        )

    def test_claude_md_names_adr_010_as_only_amendment(self, claude_text: str) -> None:
        """CLAUDE.md must name ADR-010 as the only approved I-3 amendment."""
        lower = claude_text.lower()
        assert "adr-010" in lower, (
            "CLAUDE.md must reference ADR-010"
        )
        assert "only approved" in lower, (
            "CLAUDE.md must state ADR-010 is the only approved I-3 amendment"
        )

    def test_claude_md_documents_enabled_policy(self, claude_text: str) -> None:
        """CLAUDE.md must document enabled-policy gate for the amendment."""
        lower = claude_text.lower()
        assert "refactor_transactions_enabled" in lower, (
            "CLAUDE.md must reference refactor_transactions_enabled"
        )

    def test_claude_md_documents_declared_regression(self, claude_text: str) -> None:
        """CLAUDE.md must document declared-regression tolerance."""
        lower = claude_text.lower()
        assert "declared_regression_keys" in lower, (
            "CLAUDE.md must reference declared_regression_keys"
        )

    def test_claude_md_documents_deadline_bounded(self, claude_text: str) -> None:
        """CLAUDE.md must document deadline-bounded behavior."""
        lower = claude_text.lower()
        assert "deadline" in lower, (
            "CLAUDE.md must mention deadline in I-3 amendment text"
        )

    def test_claude_md_documents_rollback_on_failure(self, claude_text: str) -> None:
        """CLAUDE.md must document rollback-on-failure."""
        lower = claude_text.lower()
        assert "rolled back" in lower or "rollback" in lower, (
            "CLAUDE.md must document rollback-on-failure in I-3 amendment"
        )

    def test_claude_md_no_score_based_commit_drift(self, claude_text: str) -> None:
        """CLAUDE.md must not drift toward score-based commits."""
        lower = claude_text.lower()
        forbidden = [
            "score-based commit",
            "score-based promotion",
            "score to decide",
            "score determines",
        ]
        for phrase in forbidden:
            assert phrase not in lower, (
                f"CLAUDE.md must not contain '{phrase}' (score-based commit drift)"
            )

    def test_claude_md_no_default_regression_tolerance_drift(
        self, claude_text: str
    ) -> None:
        """CLAUDE.md must not drift toward default regression tolerance."""
        lower = claude_text.lower()
        forbidden = [
            "regressions are tolerated by default",
            "default regression tolerance",
            "regressions allowed by default",
            "tolerate regressions by default",
        ]
        for phrase in forbidden:
            assert phrase not in lower, (
                f"CLAUDE.md must not contain '{phrase}' (default regression tolerance drift)"
            )


# ---------------------------------------------------------------------------
# VAL-CROSS-013: Formal v0.7 spec path is synchronized
# ---------------------------------------------------------------------------

class TestFormalSpecPathSync:
    """Guard for the formal v0.7 spec path.

    Asserts that:
    - The formal spec file exists at the expected path.
    - It reflects delivered scope for synthesis, discovery credit,
      refactor transactions, memory promote and recall, final gates,
      defaults, and approved baseline deselection set.
    - Only the affected placeholder specs are updated with accurate
      delivered-scope links.
    - Unrelated future placeholders are NOT marked as delivered.
    """

    @pytest.fixture
    def spec_text(self) -> str:
        assert _SPEC_PATH.exists(), (
            f"Formal v0.7 spec not found at {_SPEC_PATH}"
        )
        return _SPEC_PATH.read_text(encoding="utf-8")

    def test_spec_file_exists(self, spec_text: str) -> None:
        assert len(spec_text) > 0, "Formal v0.7 spec is empty"

    def test_spec_documents_synthesis(self, spec_text: str) -> None:
        """Spec must reflect delivered synthesis scope."""
        lower = spec_text.lower()
        assert "synthesis" in lower or "synthesizer" in lower, (
            "Spec must document spec check synthesis"
        )
        assert "spec_check_synthesizer" in lower or "specchecksynthesizer" in lower, (
            "Spec must reference the SpecCheckSynthesizer component"
        )

    def test_spec_documents_discovery_credit(self, spec_text: str) -> None:
        """Spec must reflect delivered discovery credit scope."""
        lower = spec_text.lower()
        assert "discovery" in lower and "credit" in lower, (
            "Spec must document discovery credit"
        )

    def test_spec_documents_refactor_transactions(self, spec_text: str) -> None:
        """Spec must reflect delivered refactor transaction scope."""
        lower = spec_text.lower()
        assert "refactor" in lower and "transaction" in lower, (
            "Spec must document refactor transactions"
        )

    def test_spec_documents_memory_promote_and_recall(self, spec_text: str) -> None:
        """Spec must reflect delivered memory promote and recall scope."""
        lower = spec_text.lower()
        assert "memory" in lower, "Spec must document memory"
        assert "promote" in lower or "auto_promote" in lower, (
            "Spec must document memory auto-promote"
        )
        assert "recall" in lower, "Spec must document memory recall"

    def test_spec_documents_defaults(self, spec_text: str) -> None:
        """Spec must document policy defaults preserving v0.6 behavior."""
        lower = spec_text.lower()
        assert "default" in lower, "Spec must mention defaults"
        assert "synthesis" in lower and "off" in lower, (
            "Spec must state synthesis defaults off"
        )
        assert "refactor" in lower and "off" in lower, (
            "Spec must state refactor transactions default off"
        )

    def test_spec_documents_approved_deselection_set(self, spec_text: str) -> None:
        """Spec must reference the approved baseline deselection set."""
        lower = spec_text.lower()
        assert "deselect" in lower or "baseline" in lower, (
            "Spec must reference approved baseline deselection set"
        )

    def test_spec_documents_final_gates(self, spec_text: str) -> None:
        """Spec must reference final validation gates (mypy, ruff, pytest)."""
        lower = spec_text.lower()
        assert "mypy" in lower, "Spec must reference mypy gate"
        assert "ruff" in lower, "Spec must reference ruff gate"
        assert "pytest" in lower, "Spec must reference pytest gate"

    def test_spec_references_placeholder_links(self, spec_text: str) -> None:
        """Spec must reference the affected placeholder specs."""
        lower = spec_text.lower()
        assert "llm_planner" in lower or "llm planner" in lower, (
            "Spec must reference llm_planner placeholder (partial)"
        )
        assert "concurrent_fan_out" in lower or "fan.out" in lower, (
            "Spec must reference concurrent_fan_out placeholder (commit-selection only)"
        )
        assert "cross_task_memory" in lower or "memory recall" in lower, (
            "Spec must reference cross_task_memory_recall placeholder (delivered/linked)"
        )

    def test_spec_status_reflects_delivery(self, spec_text: str) -> None:
        """Spec should reflect delivered status, not future-only planning."""
        lower = spec_text.lower()
        # Must not say "implementation plan to follow" (it's done now).
        assert "implementation plan to follow" not in lower, (
            "Spec still says 'implementation plan to follow' - should reflect delivered status"
        )

    def test_spec_documents_commit_selection(self, spec_text: str) -> None:
        """Spec must document the deterministic commit-selection interface."""
        lower = spec_text.lower()
        assert "commit" in lower and "selection" in lower, (
            "Spec must document commit selection"
        )
        assert "score" in lower, (
            "Spec must address score (to clarify it is excluded from selection)"
        )


# ---------------------------------------------------------------------------
# VAL-CROSS-013: Placeholder specs are accurately scoped
# ---------------------------------------------------------------------------

class TestPlaceholderSpecSync:
    """Guard for placeholder spec accuracy.

    Asserts that:
    - llm_planner.md is marked as partially delivered (synthesis only).
    - concurrent_fan_out_and_join.md is marked as commit-selection-only delivered.
    - cross_task_memory_recall.md is marked as delivered/linked.
    - services_yaml_rich_semantics.md is NOT marked as delivered.
    - web_ui.md is NOT marked as delivered.
    """

    def test_llm_planner_placeholder_references_delivered_scope(self) -> None:
        """llm_planner placeholder should reference the delivered synthesis scope."""
        assert _LLM_PLANNER_PLACEHOLDER.exists()
        text = _LLM_PLANNER_PLACEHOLDER.read_text(encoding="utf-8").lower()
        assert "synthesis" in text or "spec_check_synthesizer" in text or (
            "v0.7_implementation" in text
        ), (
            "llm_planner placeholder should reference delivered synthesis scope "
            "or link to the formal spec"
        )

    def test_fan_out_placeholder_references_commit_selection(self) -> None:
        """concurrent_fan_out placeholder should reference commit-selection delivery."""
        assert _FAN_OUT_PLACEHOLDER.exists()
        text = _FAN_OUT_PLACEHOLDER.read_text(encoding="utf-8").lower()
        assert "commit" in text and "selection" in text or (
            "v0.7_implementation" in text
        ), (
            "concurrent_fan_out placeholder should reference commit-selection "
            "delivery or link to the formal spec"
        )

    def test_memory_recall_placeholder_references_delivery(self) -> None:
        """cross_task_memory_recall placeholder should reference delivery."""
        assert _MEMORY_RECALL_PLACEHOLDER.exists()
        text = _MEMORY_RECALL_PLACEHOLDER.read_text(encoding="utf-8").lower()
        assert "delivered" in text or "v0.7_implementation" in text or (
            "auto_promote" in text or "recall" in text and "implemented" in text
        ), (
            "cross_task_memory_recall placeholder should reference delivered scope "
            "or link to the formal spec"
        )

    def test_services_yaml_placeholder_not_marked_delivered(self) -> None:
        """services_yaml_rich_semantics placeholder must NOT be marked as delivered."""
        assert _SERVICES_YAML_PLACEHOLDER.exists()
        text = _SERVICES_YAML_PLACEHOLDER.read_text(encoding="utf-8").lower()
        assert "delivered" not in text, (
            "services_yaml_rich_semantics placeholder must not be marked as delivered"
        )

    def test_web_ui_placeholder_not_marked_delivered(self) -> None:
        """web_ui placeholder must NOT be marked as delivered."""
        assert _WEB_UI_PLACEHOLDER.exists()
        text = _WEB_UI_PLACEHOLDER.read_text(encoding="utf-8").lower()
        assert "delivered" not in text, (
            "web_ui placeholder must not be marked as delivered"
        )
