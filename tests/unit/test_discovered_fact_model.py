from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from hungerloop.models import (
    DiscoveredFact,
    DiscoveredFactKind,
    HandoffProcessingResult,
)
from hungerloop.models.handoff import DiscoveredFact as DiscoveredFactFromModule
from hungerloop.models.handoff import (
    HandoffProcessingResult as HandoffProcessingResultFromModule,
)


def _make_discovered_fact() -> DiscoveredFact:
    return DiscoveredFact(
        kind="mission_feature",
        title="Need repository support",
        description="The repository still needs WorkerHandoff persistence methods.",
        source_handoff_id="handoff-1",
        related_feature_ids=["m2-worker-handoff-repository"],
    )


def test_discovered_fact_models_are_exported_and_documented() -> None:
    assert DiscoveredFact is DiscoveredFactFromModule
    assert HandoffProcessingResult is HandoffProcessingResultFromModule
    assert get_args(DiscoveredFactKind) == (
        "mission_feature",
        "blocker_note",
        "test_gap",
    )
    assert "REQ-M2-040" in (DiscoveredFact.__doc__ or "")
    assert "REQ-M2-020" in (HandoffProcessingResult.__doc__ or "")


def test_discovered_fact_has_documented_surface() -> None:
    fact = _make_discovered_fact()

    assert set(DiscoveredFact.model_fields) == {
        "kind",
        "title",
        "description",
        "source_handoff_id",
        "related_feature_ids",
    }
    assert fact.related_feature_ids == ["m2-worker-handoff-repository"]


def test_discovered_fact_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        DiscoveredFact(
            kind="quantum_judge",  # type: ignore[arg-type]
            title="Impossible",
            description="Unknown discovered fact kind.",
            source_handoff_id="handoff-1",
            related_feature_ids=[],
        )


def test_handoff_processing_result_exposes_expected_fields_only() -> None:
    result = HandoffProcessingResult(
        prior_handoff_summary="S" * 1200,
        discovered_issues=[_make_discovered_fact()],
        blocked_item_ids=["H-1"],
        injected_hunger_item_ids=["H-2"],
    )

    assert set(HandoffProcessingResult.model_fields) == {
        "prior_handoff_summary",
        "discovered_issues",
        "blocked_item_ids",
        "injected_hunger_item_ids",
    }
    assert len(result.prior_handoff_summary) == 800
    assert "early_stop_reason" not in HandoffProcessingResult.model_fields
    assert "stop_reason" not in HandoffProcessingResult.model_fields
    with pytest.raises(AttributeError):
        _ = result.early_stop_reason
    with pytest.raises(AttributeError):
        _ = result.stop_reason
