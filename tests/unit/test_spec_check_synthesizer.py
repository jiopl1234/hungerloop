"""Focused unit tests for SpecCheckSynthesizer.

Covers VAL-SYN-008 (parse, validate, gate model output),
VAL-SYN-009 (cost guarded and auditable),
VAL-SYN-016 (prompts use authoritative spec sources),
VAL-SYN-017 (caps and deduplication are global and exact).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.usage import ModelUsage
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.model_client import ModelResponse
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.spec_check_synthesizer import (
    SYNTHESIS_MAX_TOKENS,
    SpecCheckSynthesizer,
    build_covered_check_digest,
    build_synthesis_prompt,
    run_plan_time_synthesis,
    run_post_commit_synthesis,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCompletionClient:
    """Scriptable completion client for tests."""

    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        self._responses: list[ModelResponse] = list(responses or [])
        self._calls: int = 0
        self._last_messages: list[dict[str, str]] | None = None
        self._last_max_tokens: int | None = None

    @property
    def call_count(self) -> int:
        return self._calls

    @property
    def last_messages(self) -> list[dict[str, str]] | None:
        return self._last_messages

    @property
    def last_max_tokens(self) -> int | None:
        return self._last_max_tokens

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        self._calls += 1
        self._last_messages = messages
        self._last_max_tokens = max_tokens
        if self._responses:
            return self._responses.pop(0)
        return ModelResponse(
            content="[]",
            json_data={},
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        )


class _SpyCostGuard:
    """CostGuard spy that records before/after calls."""

    def __init__(self, fail_on_after: bool = False) -> None:
        self.before_calls: list[str] = []
        self.after_calls: list[str] = []
        self._fail_on_after = fail_on_after
        self._call_count = 0
        self.recorded_usage: list[ModelUsage] = []

    def assert_within_budget(self, task_id: str) -> None:
        self._call_count += 1
        if self._call_count == 1:
            self.before_calls.append(task_id)
        else:
            self.after_calls.append(task_id)
            if self._fail_on_after:
                raise SafetyStopError("budget exceeded after call")

    def record_llm_usage(self, task_id: str, usage: ModelUsage) -> None:
        self.recorded_usage.append(usage)


class _FailingCompletionClient:
    """Completion client that always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self._calls = 0

    @property
    def call_count(self) -> int:
        return self._calls

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        self._calls += 1
        raise self._exc


class _AlwaysPassDryRunner:
    async def dry_run(self, argv: list[str], cwd: Any = None) -> bool:
        del argv, cwd
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MISSION_PROSE = "The project must have a main.py file and pass pytest."
_FEATURE_DESCS = ["Feature: main module at src/main.py", "Feature: test suite passes"]


def _make_response(content: str) -> ModelResponse:
    return ModelResponse(
        content=content,
        usage=ModelUsage(input_tokens=10, output_tokens=10, cost_usd=0.001),
    )


def _make_file_proposal_json(
    path: str = "src/main.py",
    description: str = "main file exists",
    source_quote: str = "The project must have a main.py file",
) -> dict[str, Any]:
    return {
        "check_type": "file_exists",
        "params": {"path": path},
        "description": description,
        "source_quote": source_quote,
    }


def _make_shell_proposal_json(
    argv: list[str] | None = None,
    description: str = "run tests",
    source_quote: str = "pass pytest",
) -> dict[str, Any]:
    return {
        "check_type": "shell_exit_zero",
        "params": {"argv": argv or ["python", "-m", "pytest"]},
        "description": description,
        "source_quote": source_quote,
    }


def _setup_repo(
    *,
    task_id: str = "t1",
    items: list[HungerItem] | None = None,
    policy: HungerPolicy | None = None,
) -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.create_task(task_id, "test goal")
    repo.set_hunger_policy(task_id, policy or HungerPolicy())
    repo.save_hunger_ledger(
        task_id, HungerLedger(task_id=task_id, items=items or [])
    )
    return repo


def _make_synthesizer(
    *,
    repo: InMemoryRepository,
    completion_client: Any,
    cost_guard: Any | None = None,
    gate: CheckProposalGate | None = None,
) -> SpecCheckSynthesizer:
    return SpecCheckSynthesizer(
        repo=repo,
        cost_guard=cost_guard or CostGuard(repo),
        completion_client=completion_client,
        gate=gate or CheckProposalGate(),
        model_name="test-model",
    )


# ---------------------------------------------------------------------------
# VAL-SYN-008: Parse, validate, and gate model output
# ---------------------------------------------------------------------------


class TestSynthesisParsesValidatesGates:
    """Tests proving synthesis handles raw JSON, fenced JSON, bad JSON,
    bad proposal objects, and gate rejections without crashing."""

    @pytest.mark.asyncio
    async def test_raw_json_array_accepted(self) -> None:
        repo = _setup_repo()
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.completion_called
        assert result.accepted_count == 1
        assert result.accepted_proposals[0].check_type == AcceptanceCheckType.FILE_EXISTS

    @pytest.mark.asyncio
    async def test_fenced_json_accepted(self) -> None:
        repo = _setup_repo()
        proposals_json = f"```json\n{json.dumps([_make_file_proposal_json()])}\n```"
        client = _FakeCompletionClient([_make_response(proposals_json)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 1

    @pytest.mark.asyncio
    async def test_garbage_output_no_crash(self) -> None:
        repo = _setup_repo()
        client = _FakeCompletionClient([_make_response("not json at all")])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0
        assert result.rejected_count >= 1
        # SYNTH_CHECK_REJECTED event emitted
        events = repo.list_events("t1")
        rejection_events = [
            e for e in events if e["event_type"] == EventType.SYNTH_CHECK_REJECTED.value
        ]
        assert len(rejection_events) >= 1

    @pytest.mark.asyncio
    async def test_non_list_json_no_crash(self) -> None:
        repo = _setup_repo()
        client = _FakeCompletionClient([_make_response('{"not": "a list"}')])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0

    @pytest.mark.asyncio
    async def test_disallowed_check_type_dropped(self) -> None:
        repo = _setup_repo()
        bad_proposal = {
            "check_type": "llm_judge",
            "params": {},
            "description": "bad",
            "source_quote": "The project must have a main.py file",
        }
        client = _FakeCompletionClient([_make_response(json.dumps([bad_proposal]))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0

    @pytest.mark.asyncio
    async def test_bad_proposal_object_dropped(self) -> None:
        repo = _setup_repo()
        # Missing source_quote
        bad_proposal = {
            "check_type": "file_exists",
            "params": {"path": "src/main.py"},
            "description": "main file",
        }
        client = _FakeCompletionClient([_make_response(json.dumps([bad_proposal]))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0

    @pytest.mark.asyncio
    async def test_gate_rejection_handled(self) -> None:
        repo = _setup_repo()
        # file_exists with traversal path -> gate rejects
        bad_path_proposal = {
            "check_type": "file_exists",
            "params": {"path": "../../../etc/passwd"},
            "description": "traversal",
            "source_quote": "The project must have a main.py file",
        }
        raw_response = json.dumps([bad_path_proposal])
        client = _FakeCompletionClient([_make_response(raw_response)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0
        assert result.rejected_count >= 1
        events = repo.list_events(
            "t1", event_types=[EventType.SYNTH_CHECK_REJECTED.value]
        )
        assert len(events) == 1
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["reason"] == "gate_rejection"
        assert payload["raw_response_excerpt"] == raw_response
        rejected = payload["rejected_proposals"]
        assert isinstance(rejected, list)
        assert rejected[0]["reason"] == "unsafe_path:traversal"
        assert rejected[0]["source_quote"] == bad_path_proposal["source_quote"]

    @pytest.mark.asyncio
    async def test_unanchored_source_quote_rejected(self) -> None:
        repo = _setup_repo()
        # Source quote not in mission prose
        proposal = {
            "check_type": "file_exists",
            "params": {"path": "src/main.py"},
            "description": "main file",
            "source_quote": "This quote does not appear in the spec text.",
        }
        client = _FakeCompletionClient([_make_response(json.dumps([proposal]))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0


    @pytest.mark.asyncio
    async def test_whitespace_normalized_source_quote_is_accepted(self) -> None:
        repo = _setup_repo()
        proposal = {
            "check_type": "file_exists",
            "params": {"path": "src/main.py"},
            "description": "main file",
            "source_quote": "The project must have a\nmain.py file",
        }
        client = _FakeCompletionClient([_make_response(json.dumps([proposal]))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 1

    @pytest.mark.asyncio
    async def test_rejection_event_includes_per_proposal_details_and_raw_response(self) -> None:
        repo = _setup_repo()
        proposal = {
            "check_type": "file_exists",
            "params": {"path": "src/main.py"},
            "description": "main file",
            "source_quote": "This quote does not appear in the spec text.",
        }
        raw_response = json.dumps([proposal])
        client = _FakeCompletionClient([_make_response(raw_response)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=7,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0
        events = repo.list_events(
            "t1", event_types=[EventType.SYNTH_CHECK_REJECTED.value]
        )
        assert len(events) == 1
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["reason"] == "no_valid_proposals"
        assert payload["raw_response_excerpt"] == raw_response
        rejected = payload["rejected_proposals"]
        assert isinstance(rejected, list)
        assert rejected[0]["reason"] == "source_quote_unanchored"
        assert rejected[0]["source_quote"] == proposal["source_quote"]


# ---------------------------------------------------------------------------
# VAL-SYN-009: Cost guarded and auditable
# ---------------------------------------------------------------------------


class TestSynthesisCostGuarded:
    """Tests proving every synthesis call has CostGuard before and after,
    and failure cases fail closed with non-secret audit events."""

    @pytest.mark.asyncio
    async def test_cost_guard_before_and_after(self) -> None:
        repo = _setup_repo()
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        spy = _SpyCostGuard()
        synth = SpecCheckSynthesizer(
            repo=repo,
            cost_guard=spy,  # type: ignore[arg-type]
            completion_client=client,
            gate=CheckProposalGate(),
            model_name="test-model",
        )

        await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert len(spy.before_calls) == 1
        assert spy.before_calls[0] == "t1"
        assert len(spy.after_calls) == 1
        assert spy.after_calls[0] == "t1"

    @pytest.mark.asyncio
    async def test_exactly_one_completion_call(self) -> None:
        repo = _setup_repo()
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert client.call_count == 1
        assert client.last_max_tokens == SYNTHESIS_MAX_TOKENS

    @pytest.mark.asyncio
    async def test_completion_error_fails_closed(self) -> None:
        repo = _setup_repo()
        client = _FailingCompletionClient(RuntimeError("network error"))
        spy = _SpyCostGuard()
        synth = SpecCheckSynthesizer(
            repo=repo,
            cost_guard=spy,  # type: ignore[arg-type]
            completion_client=client,
            gate=CheckProposalGate(),
            model_name="test-model",
        )

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0
        assert result.completion_called
        events = repo.list_events("t1")
        rejection_events = [
            e for e in events if e["event_type"] == EventType.SYNTH_CHECK_REJECTED.value
        ]
        assert len(rejection_events) >= 1
        # Payload must not contain secrets
        payload = rejection_events[0]["payload"]
        assert "api_key" not in str(payload).lower()
        assert "bearer" not in str(payload).lower()

    @pytest.mark.asyncio
    async def test_safety_stop_propagates(self) -> None:
        repo = _setup_repo()
        spy = _SpyCostGuard(fail_on_after=True)
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        synth = SpecCheckSynthesizer(
            repo=repo,
            cost_guard=spy,  # type: ignore[arg-type]
            completion_client=client,
            gate=CheckProposalGate(),
            model_name="test-model",
        )

        with pytest.raises(SafetyStopError):
            await synth.synthesize(
                task_id="t1",
                loop_id=None,
                mission_prose=_MISSION_PROSE,
                feature_descriptions=_FEATURE_DESCS,
                synthesis_max_total_items=20,
            )

    @pytest.mark.asyncio
    async def test_zero_capacity_skips_model_call(self) -> None:
        repo = _setup_repo()
        client = _FakeCompletionClient([_make_response("[]")])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=0,
        )

        assert result.skipped
        assert client.call_count == 0
        events = repo.list_events("t1")
        skip_events = [
            e for e in events if e["event_type"] == EventType.SYNTHESIS_SKIPPED.value
        ]
        assert len(skip_events) >= 1


# ---------------------------------------------------------------------------
# VAL-SYN-016: Synthesis prompts use authoritative spec sources
# ---------------------------------------------------------------------------


class TestSynthesisPromptSources:
    """Tests proving the synthesizer prompt is built from mission sources only."""

    def test_prompt_contains_mission_prose(self) -> None:
        messages = build_synthesis_prompt(
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
        )
        assert len(messages) == 2
        assert _MISSION_PROSE in messages[1]["content"]
        for desc in _FEATURE_DESCS:
            assert desc in messages[1]["content"]

    def test_prompt_includes_covered_check_digest(self) -> None:
        digest = "H-001: file_exists (main file)"
        messages = build_synthesis_prompt(
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            covered_check_digest=digest,
        )
        assert digest in messages[1]["content"]

    def test_prompt_system_message_specifies_json_array(self) -> None:
        messages = build_synthesis_prompt(
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
        )
        system = messages[0]["content"]
        assert "JSON" in system
        assert "shell_exit_zero" in system or "file_exists" in system
        assert '"argv": ["python", "-c", "..."]' in system
        assert "never a single command string" in system

    @pytest.mark.asyncio
    async def test_source_quote_must_be_anchored(self) -> None:
        repo = _setup_repo()
        # Valid proposal with anchored quote
        good = _make_file_proposal_json(
            source_quote="The project must have a main.py file"
        )
        # Proposal with unanchored quote
        bad = _make_file_proposal_json(
            path="src/other.py",
            source_quote="This text does not exist in the spec.",
        )
        client = _FakeCompletionClient(
            [_make_response(json.dumps([good, bad]))]
        )
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 1
        assert result.accepted_proposals[0].params["path"] == "src/main.py"


# ---------------------------------------------------------------------------
# VAL-SYN-017: Caps and deduplication are global and exact
# ---------------------------------------------------------------------------


class TestSynthesisCapsAndDedup:
    """Tests proving synthesis caps and deduplication are global and exact."""

    @pytest.mark.asyncio
    async def test_existing_h_syn_items_consume_capacity(self) -> None:
        existing_item = HungerItem(
            id="H-SYN-001",
            title="existing",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/existing.py"},
                    description="existing",
                )
            ],
            refinement_kind="spec_coverage",
            generated_by="synthesizer",
        )
        repo = _setup_repo(items=[existing_item])
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        # Capacity = 20 - 1 existing = 19 remaining
        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 1  # The new proposal is accepted

    @pytest.mark.asyncio
    async def test_capacity_exhausted_skips_model_call(self) -> None:
        existing_item = HungerItem(
            id="H-SYN-001",
            title="existing",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/existing.py"},
                    description="existing",
                )
            ],
            refinement_kind="spec_coverage",
            generated_by="synthesizer",
        )
        repo = _setup_repo(items=[existing_item])
        client = _FakeCompletionClient([_make_response("[]")])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        # Capacity = 1 - 1 existing = 0 remaining
        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=1,
        )

        assert result.skipped
        assert client.call_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_against_existing_check_not_accepted(self) -> None:
        """Operator-authored check should block duplicate synthesis."""
        existing_item = HungerItem(
            id="H-001",
            title="operator check",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/main.py"},
                    description="operator-authored check",
                )
            ],
        )
        repo = _setup_repo(items=[existing_item])
        # Same path as existing check
        proposal = _make_file_proposal_json(path="src/main.py")
        client = _FakeCompletionClient([_make_response(json.dumps([proposal]))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 0
        # But the model was called (capacity was not zero)
        assert result.completion_called

    @pytest.mark.asyncio
    async def test_operator_check_does_not_consume_synthesis_capacity(self) -> None:
        """Operator-authored checks should not consume synthesis_max_total_items."""
        existing_item = HungerItem(
            id="H-001",
            title="operator check",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/existing.py"},
                    description="operator check",
                )
            ],
        )
        repo = _setup_repo(items=[existing_item])
        client = _FakeCompletionClient([_make_response("[]")])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        # Capacity = 1. If operator check consumed capacity, remaining=0 and
        # model call would be skipped. But operator checks don't consume
        # synthesis capacity, so remaining=1 and model is called.
        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=1,
        )

        assert not result.skipped
        assert result.completion_called

    @pytest.mark.asyncio
    async def test_accepted_proposals_capped_at_remaining(self) -> None:
        existing_item = HungerItem(
            id="H-SYN-001",
            title="existing",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/existing.py"},
                    description="existing",
                )
            ],
            refinement_kind="spec_coverage",
            generated_by="synthesizer",
        )
        repo = _setup_repo(items=[existing_item])
        # 3 proposals, but capacity = 20 - 1 = 19, so all 3 accepted
        proposals = [
            _make_file_proposal_json(path=f"src/file{i}.py") for i in range(3)
        ]
        client = _FakeCompletionClient([_make_response(json.dumps(proposals))])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert result.accepted_count == 3

    @pytest.mark.asyncio
    async def test_zero_max_items_no_model_call(self) -> None:
        repo = _setup_repo()
        client = _FakeCompletionClient([_make_response("[]")])
        synth = _make_synthesizer(repo=repo, completion_client=client)

        result = await synth.synthesize(
            task_id="t1",
            loop_id=None,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=0,
        )

        assert result.skipped
        assert client.call_count == 0


# ---------------------------------------------------------------------------
# Plan-time and post-commit synthesis helpers
# ---------------------------------------------------------------------------


class TestPlanTimeSynthesis:
    """Tests for run_plan_time_synthesis helper."""

    @pytest.mark.asyncio
    async def test_plan_time_synthesis_injects_through_compiler(self) -> None:
        repo = _setup_repo()
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        compiler = RefinementCompiler(repo)

        injected = await run_plan_time_synthesis(
            task_id="t1",
            repo=repo,
            cost_guard=CostGuard(repo),
            completion_client=client,
            gate=CheckProposalGate(),
            refinement_compiler=compiler,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_plan_time_tier=0,
            synthesis_max_total_items=20,
            model_name="test-model",
        )

        assert len(injected) == 1
        assert injected[0].startswith("H-SYN-")
        # Verify the item is in the ledger
        ledger = repo.get_hunger_ledger("t1")
        assert any(item.id == injected[0] for item in ledger.items)
        injected_item = next(item for item in ledger.items if item.id == injected[0])
        assert injected_item.synthesis_baseline_pending is True

    @pytest.mark.asyncio
    async def test_plan_time_no_proposals_returns_empty(self) -> None:
        repo = _setup_repo()
        client = _FakeCompletionClient([_make_response("[]")])
        compiler = RefinementCompiler(repo)

        injected = await run_plan_time_synthesis(
            task_id="t1",
            repo=repo,
            cost_guard=CostGuard(repo),
            completion_client=client,
            gate=CheckProposalGate(),
            refinement_compiler=compiler,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_plan_time_tier=0,
            synthesis_max_total_items=20,
        )

        assert injected == []


class TestPostCommitSynthesis:
    """Tests for run_post_commit_synthesis helper."""

    @pytest.mark.asyncio
    async def test_post_commit_injects_through_compiler(self) -> None:
        repo = _setup_repo()
        proposals_json = json.dumps([_make_file_proposal_json()])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        compiler = RefinementCompiler(repo)

        injected = await run_post_commit_synthesis(
            task_id="t1",
            loop_id=1,
            repo=repo,
            cost_guard=CostGuard(repo),
            completion_client=client,
            gate=CheckProposalGate(),
            refinement_compiler=compiler,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_max_total_items=20,
        )

        assert len(injected) == 1
        assert injected[0].startswith("H-SYN-")


class TestCoveredCheckDigest:
    """Tests for build_covered_check_digest."""

    def test_empty_ledger(self) -> None:
        repo = _setup_repo()
        digest = build_covered_check_digest(repo=repo, task_id="t1")
        assert "no existing checks" in digest

    def test_with_items(self) -> None:
        item = HungerItem(
            id="H-001",
            title="test",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/main.py"},
                    description="main file",
                )
            ],
        )
        repo = _setup_repo(items=[item])
        digest = build_covered_check_digest(repo=repo, task_id="t1")
        assert "H-001" in digest
        assert "file_exists" in digest
        assert "main file" in digest

    def test_preloaded_rejected_keys_avoid_event_rescan(self) -> None:
        repo = _setup_repo()
        repo.list_events = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("event history should not be rescanned")
        )

        digest = build_covered_check_digest(
            repo=repo,
            task_id="t1",
            rejected_dedup_keys={"file_exists:rejected.txt"},
        )

        assert "rejected proposal: file_exists:rejected.txt" in digest


# ---------------------------------------------------------------------------
# VAL-SYN-012: Synthesized checks extend task completion semantics
# ---------------------------------------------------------------------------


class TestSynthesizedChecksExtendDoneSemantics:
    """Tests proving synthesized items affect DONE semantics."""

    @pytest.mark.asyncio
    async def test_synthesized_item_prevents_done(self) -> None:
        """When synthesis injects an open H-SYN item, the task is not DONE."""
        from hungerloop.models.enums import HungerItemStatus

        # Original check is CLOSED (done), synthesized check is OPEN
        original_item = HungerItem(
            id="H-001",
            title="original",
            status=HungerItemStatus.CLOSED,
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/main.py"},
                    description="original",
                )
            ],
        )
        repo = _setup_repo(items=[original_item])

        # Without synthesis, ledger.is_done() is True
        ledger = repo.get_hunger_ledger("t1")
        assert ledger.is_done()

        # Run plan-time synthesis that injects a new item
        proposals_json = json.dumps([_make_file_proposal_json(path="src/new.py")])
        client = _FakeCompletionClient([_make_response(proposals_json)])
        compiler = RefinementCompiler(repo)

        await run_plan_time_synthesis(
            task_id="t1",
            repo=repo,
            cost_guard=CostGuard(repo),
            completion_client=client,
            gate=CheckProposalGate(),
            refinement_compiler=compiler,
            mission_prose=_MISSION_PROSE,
            feature_descriptions=_FEATURE_DESCS,
            synthesis_plan_time_tier=0,
            synthesis_max_total_items=20,
        )

        # After synthesis, ledger.is_done() should be False
        ledger = repo.get_hunger_ledger("t1")
        assert not ledger.is_done()


def test_prompt_requires_fixture_assertion_split() -> None:
    messages = build_synthesis_prompt(
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
    )
    system = messages[0]["content"]
    assert "fixture_argv" in system
    assert "setup only" in system
    assert "must not contain the assertion" in system


async def test_active_item_backpressure_skips_model_call() -> None:
    active = HungerItem(
        id="H-SYN-001",
        title="active synthesized check",
        generated_by="synthesizer",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "pending.txt"},
            )
        ],
    )
    repo = _setup_repo(items=[active])
    client = _FakeCompletionClient([_make_response("[]")])
    synth = _make_synthesizer(repo=repo, completion_client=client)

    result = await synth.synthesize(
        task_id="t1",
        loop_id=1,
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
        synthesis_max_total_items=20,
        synthesis_max_active_items=1,
    )

    assert result.skipped is True
    assert client.call_count == 0


async def test_rejected_key_is_sticky_and_does_not_consume_batch_slot() -> None:
    repo = _setup_repo()
    rejected = CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": "src/main.py"},
        description="previously rejected",
        source_quote="The project must have a main.py file",
        proposed_by="synthesizer",
    )
    repo.append_event(
        EventType.SYNTH_CHECK_REJECTED,
        {"dedup_keys": [rejected.dedup_key()]},
        task_id="t1",
    )
    client = _FakeCompletionClient(
        [
            _make_response(
                json.dumps(
                    [
                        _make_file_proposal_json(),
                        _make_file_proposal_json(
                            path="fresh.py",
                            description="fresh file",
                            source_quote="Feature: main module at src/main.py",
                        ),
                    ]
                )
            )
        ]
    )
    synth = _make_synthesizer(repo=repo, completion_client=client)

    result = await synth.synthesize(
        task_id="t1",
        loop_id=1,
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
        synthesis_max_total_items=20,
        synthesis_batch_size=1,
    )

    assert [proposal.params["path"] for proposal in result.accepted_proposals] == [
        "fresh.py"
    ]


async def test_explicit_semantic_audit_rejection_is_filtered() -> None:
    repo = _setup_repo()
    raw = _make_file_proposal_json()
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params=raw["params"],
        description=str(raw["description"]),
        source_quote=str(raw["source_quote"]),
        proposed_by="synthesizer",
    )
    client = _FakeCompletionClient(
        [
            _make_response(json.dumps([raw])),
            _make_response(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "dedup_key": proposal.dedup_key(),
                                "entailed": False,
                                "reason": "assertion exceeds the quoted spec",
                            }
                        ]
                    }
                )
            ),
        ]
    )
    synth = _make_synthesizer(repo=repo, completion_client=client)

    result = await synth.synthesize(
        task_id="t1",
        loop_id=1,
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
        synthesis_max_total_items=20,
        synthesis_audit_enabled=True,
    )

    assert result.accepted_proposals == []
    assert client.call_count == 2
    assert repo.list_events(
        "t1",
        event_types=[EventType.SYNTH_CHECK_AUDIT_REJECTED.value],
    )
    evidence = repo.list_evidence("t1", evidence_type="model_call")
    assert [row["agent_id"] for row in evidence] == [
        "spec_check_synthesizer",
        "spec_entailment_auditor",
    ]
    assert "assertion exceeds the quoted spec" in str(
        evidence[1]["response_preview"]
    )


async def test_unparseable_semantic_audit_fails_open() -> None:
    repo = _setup_repo()
    client = _FakeCompletionClient(
        [
            _make_response(json.dumps([_make_file_proposal_json()])),
            _make_response("not-json"),
        ]
    )
    synth = _make_synthesizer(repo=repo, completion_client=client)

    result = await synth.synthesize(
        task_id="t1",
        loop_id=1,
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
        synthesis_max_total_items=20,
        synthesis_audit_enabled=True,
    )

    assert len(result.accepted_proposals) == 1
    assert repo.list_events(
        "t1",
        event_types=[EventType.SYNTH_AUDIT_FAILED_OPEN.value],
    )


async def test_proposal_v2_metadata_is_parsed() -> None:
    repo = _setup_repo()
    raw = _make_shell_proposal_json(
        argv=["python", "-c", "print('assertion')"],
    )
    raw["fixture_argv"] = ["python", "-c", "print('fixture')"]
    raw["prerequisite_check_keys"] = ["H-001:0"]
    client = _FakeCompletionClient([_make_response(json.dumps([raw]))])
    synth = _make_synthesizer(
        repo=repo,
        completion_client=client,
        gate=CheckProposalGate(dry_runner=_AlwaysPassDryRunner()),
    )

    result = await synth.synthesize(
        task_id="t1",
        loop_id=1,
        mission_prose=_MISSION_PROSE,
        feature_descriptions=_FEATURE_DESCS,
        synthesis_max_total_items=20,
    )

    proposal = result.accepted_proposals[0]
    assert proposal.fixture_argv == ["python", "-c", "print('fixture')"]
    assert proposal.prerequisite_check_keys == ["H-001:0"]
