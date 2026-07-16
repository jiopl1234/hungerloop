# Loop-Objective Evolution (v0.7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the three-phase design in `specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md`: (1) LLM-assisted spec-to-check synthesis with a deterministic proposal gate, (2) refactor transactions (bounded non-monotonicity) plus a fan-out-ready deterministic commit-selection function, (3) worker discovery credit and layer-3 memory auto-promote + cross-task recall.

**Architecture:** All LLM usage stays at planning/synthesis layer (never under `services/validators/`). All ledger writes route through `RequirementCompiler`/`RefinementCompiler`. New behavior is policy-flag-gated on `HungerPolicy`; defaults keep v0.6 behavior byte-identical (synthesis off, transactions off, memory promote/recall on but additive-only).

**Tech Stack:** Python 3.11+, Pydantic v2, pytest + pytest-asyncio (auto mode), mypy --strict, ruff, SQLite forward-only migrations (`PRAGMA user_version`).

## Global Constraints

- Every new module starts with `from __future__ import annotations`.
- `mypy --strict src/` must stay clean; use `X | None`, never `Optional[X]`.
- Models are data containers (no business methods beyond trivial derivations); services take `repo: RepositoryProtocol` via DI.
- CI lint contract: no LLM / `ModelClient` imports under `services/validators/`; ledger writes only inside `requirement_compiler.py` / `refinement_compiler.py`; `mission_state_updater.py` untouched.
- No score-based commit logic anywhere (I-3). `BestState.score` stays 0.0.
- New `HungerPolicy` defaults: `synthesis_enabled=False`, `synthesis_plan_time_tier=0`, `synthesis_max_total_items=20`, `refactor_transactions_enabled=False`, `max_declared_regressions=5`, `refactor_deadline_loops=3`, `memory_auto_promote_enabled=True`, `memory_recall_enabled=True`.
- New events use plain string event types (precedent: `"DISCOVERED_FACT_REJECTED"` in `handoff_processor.py`): `"SYNTH_CHECK_REJECTED"`, `"DISCOVERY_CREDIT"`, `"REFACTOR_TXN_OPENED"`, `"REFACTOR_TXN_CLOSED_SUCCESS"`, `"REFACTOR_TXN_ROLLED_BACK"`.
- Worker-generated hunger items use `generated_by=f"worker:{agent_id}"`; synthesizer uses `generated_by="spec_check_synthesizer"`.
- Baseline: `pytest tests/unit/ -q` currently ~1096 passed, 1 pre-existing failure (`test_loop_orchestrator.py::test_orchestrator_uses_validation_pipeline_and_commit_receives_result`). Do NOT fix it; do not add to it.
- Commit style: `feat:` / `test:` / `docs:` prefixes, one commit per task.

## File Structure

```
src/hungerloop/
  models/
    synthesis.py            # NEW: CheckProposal (shared Phase 1 + 3a)
    refactor.py             # NEW: RefactorTransaction
    hunger.py               # MODIFY: HungerPolicy new flag fields
    worker.py               # MODIFY: HandoffItem.proposed_checks; HandoffItemType += refactor_proposal
    context.py              # MODIFY: ContextPack.recalled_memories
    handoff.py              # MODIFY: HandoffProcessingResult.accepted_proposal_count
  services/
    check_proposal_gate.py  # NEW: deterministic proposal gate (argv allowlist, path rules, dry-run x2)
    spec_check_synthesizer.py # NEW: LLM synthesis (plan-time + incremental)
    commit_selection.py     # NEW: select_commit_candidate (fan-out-ready, no score)
    refactor_transaction_manager.py # NEW: open/settle/rollback
    refinement_compiler.py  # MODIFY: compile_spec_coverage()
    handoff_processor.py    # MODIFY: proposed_checks routing; refactor_proposal routing
    commit_manager.py       # MODIFY: txn-aware _can_commit; DISCOVERY_CREDIT emission
    stagnation_detector.py  # MODIFY: regression_exempt_item_ids
    memory_manager.py       # MODIFY: content upgrade; auto_promote()
    context_builder.py      # MODIFY: recalled_memories recall
    loop_orchestrator.py    # MODIFY: wiring (post-commit synth, txn settle, streak reset, auto_promote)
    stop_report_builder.py  # MODIFY: discovery_credits summary
  repository/
    protocol.py             # MODIFY: txn + memory-list + event-list methods
    in_memory_repo.py       # MODIFY: same
    sqlite_repo.py          # MODIFY: same
    migrations/             # NEW migration: refactor_transactions table
  cli/orchestrator_factory.py # MODIFY: wire synthesizer/gate/txn-manager adapters
docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md # NEW
CLAUDE.md                   # MODIFY: I-3 amendment
```

Read the design spec in full before starting any task.

---

## Phase 1 — Spec-to-check synthesis

### Task 1: `CheckProposal` model

**Files:**
- Create: `src/hungerloop/models/synthesis.py`
- Test: `tests/unit/test_check_proposal.py`

**Interfaces:**
- Produces: `CheckProposal(check_type, params, description, source_quote, proposed_by)` with method `dedup_key() -> str`; constant `ALLOWED_PROPOSAL_CHECK_TYPES`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_check_proposal.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import ALLOWED_PROPOSAL_CHECK_TYPES, CheckProposal


def _shell(argv: list[str]) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv},
        description="run one judge test",
        source_quote="Groups are numbered by opening parenthesis.",
        proposed_by="spec_check_synthesizer",
    )


def test_allowed_types_are_shell_and_file_only():
    assert ALLOWED_PROPOSAL_CHECK_TYPES == frozenset(
        {AcceptanceCheckType.SHELL_EXIT_ZERO, AcceptanceCheckType.FILE_EXISTS}
    )


def test_llm_judge_type_rejected():
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.LLM_JUDGE,
            params={},
            description="",
            source_quote="x",
            proposed_by="p",
        )


def test_empty_source_quote_rejected():
    with pytest.raises(ValidationError):
        CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "out.py"},
            description="",
            source_quote="   ",
            proposed_by="p",
        )


def test_dedup_key_normalizes_whitespace_and_case():
    a = _shell(["python", "-m", "pytest", "T.py::t_x"])
    b = _shell(["PYTHON", " -m ", "pytest", "t.py::T_X"])
    assert a.dedup_key() == b.dedup_key()


def test_dedup_key_differs_across_types():
    f = CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": "python"},
        description="",
        source_quote="q",
        proposed_by="p",
    )
    assert f.dedup_key() != _shell(["python"]).dedup_key()
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python -m pytest tests/unit/test_check_proposal.py -q`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'hungerloop.models.synthesis'`

- [ ] **Step 3: Implement**

```python
# src/hungerloop/models/synthesis.py
"""CheckProposal — a candidate deterministic acceptance check.

Shared by the plan-time/post-commit SpecCheckSynthesizer (Phase 1) and
worker discovery handoffs (Phase 3a). Only deterministic check types
are representable; LLM_JUDGE and friends are rejected at model level.
"""
from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field, field_validator

from hungerloop.models.enums import AcceptanceCheckType

ALLOWED_PROPOSAL_CHECK_TYPES: frozenset[AcceptanceCheckType] = frozenset(
    {AcceptanceCheckType.SHELL_EXIT_ZERO, AcceptanceCheckType.FILE_EXISTS}
)


class CheckProposal(BaseModel):
    check_type: AcceptanceCheckType
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    source_quote: str
    proposed_by: str

    @field_validator("check_type")
    @classmethod
    def _allowed_type(cls, value: AcceptanceCheckType) -> AcceptanceCheckType:
        if value not in ALLOWED_PROPOSAL_CHECK_TYPES:
            raise ValueError(f"proposal check_type not allowed: {value.value}")
        return value

    @field_validator("source_quote")
    @classmethod
    def _non_empty_quote(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_quote must be non-empty")
        return stripped

    def dedup_key(self) -> str:
        raw = self.params.get("argv", self.params.get("path", ""))
        if isinstance(raw, list):
            canon = " ".join(str(part).strip().lower() for part in raw)
        else:
            canon = " ".join(str(raw).split()).lower()
        digest = hashlib.sha256(
            f"{self.check_type.value}|{canon}".encode("utf-8")
        ).hexdigest()
        return digest[:16]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/unit/test_check_proposal.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/hungerloop/models/synthesis.py tests/unit/test_check_proposal.py
git commit -m "feat: add CheckProposal model for deterministic check proposals"
```

### Task 2: `HungerPolicy` flag fields

**Files:**
- Modify: `src/hungerloop/models/hunger.py` (class `HungerPolicy`, starts line ~150)
- Test: `tests/unit/test_hunger_policy_v07_flags.py`

**Interfaces:**
- Produces: the eight fields listed in Global Constraints, all with those exact defaults, on `HungerPolicy`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_hunger_policy_v07_flags.py
from __future__ import annotations

from hungerloop.models.hunger import HungerPolicy


def test_v07_defaults_keep_v06_behavior():
    p = HungerPolicy()
    assert p.synthesis_enabled is False
    assert p.synthesis_plan_time_tier == 0
    assert p.synthesis_max_total_items == 20
    assert p.refactor_transactions_enabled is False
    assert p.max_declared_regressions == 5
    assert p.refactor_deadline_loops == 3
    assert p.memory_auto_promote_enabled is True
    assert p.memory_recall_enabled is True
```

Note: if `HungerPolicy()` requires args in this codebase, construct it exactly the way `tests/unit/test_hunger_engine.py` constructs one, and only assert the eight new fields.

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/unit/test_hunger_policy_v07_flags.py -q` → FAIL (`AttributeError`/`ValidationError`).

- [ ] **Step 3: Implement** — append to `HungerPolicy` in `src/hungerloop/models/hunger.py`:

```python
    # v0.7 loop-objective evolution flags (design 2026-07-07)
    synthesis_enabled: bool = False
    synthesis_plan_time_tier: int = 0
    synthesis_max_total_items: int = 20
    refactor_transactions_enabled: bool = False
    max_declared_regressions: int = 5
    refactor_deadline_loops: int = 3
    memory_auto_promote_enabled: bool = True
    memory_recall_enabled: bool = True
```

- [ ] **Step 4: Run** — the new test passes AND `python -m pytest tests/unit/ -q` shows no new failures (policy round-trips through SQLite as JSON payload; if `sqlite_repo.py` uses an explicit column list for policy, extend it the same way existing fields are stored).

- [ ] **Step 5: Commit** — `git commit -m "feat: add v0.7 policy flags (synthesis, refactor txn, memory)"`

### Task 3: `CheckProposalGate`

**Files:**
- Create: `src/hungerloop/services/check_proposal_gate.py`
- Test: `tests/unit/test_check_proposal_gate.py`

**Interfaces:**
- Consumes: `CheckProposal` (Task 1).
- Produces: `class CheckProposalGate` with `async def filter(self, proposals: list[CheckProposal], *, existing_dedup_keys: set[str]) -> GateResult`; `GateResult(accepted: list[CheckProposal], rejected: list[tuple[CheckProposal, str]])`; `class DryRunner(Protocol)` with `async def run(self, argv: list[str], timeout_seconds: int) -> int` (returns exit code). Orchestrator wiring (Task 5) adapts `SandboxRunner` to `DryRunner`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_check_proposal_gate.py
from __future__ import annotations

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.services.check_proposal_gate import CheckProposalGate, GateResult


class FakeRunner:
    def __init__(self, exit_codes: list[int]) -> None:
        self.exit_codes = exit_codes
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], timeout_seconds: int) -> int:
        self.calls.append(argv)
        return self.exit_codes[min(len(self.calls) - 1, len(self.exit_codes) - 1)]


def _shell(argv: list[str]) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv}, description="d", source_quote="q", proposed_by="p",
    )


def _file(path: str) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path}, description="d", source_quote="q", proposed_by="p",
    )


async def test_accepts_deterministic_allowlisted_shell():
    gate = CheckProposalGate(FakeRunner([1, 1]))
    result = gate_result = await gate.filter([_shell(["python", "-m", "pytest", "x.py"])], existing_dedup_keys=set())
    assert isinstance(gate_result, GateResult)
    assert len(result.accepted) == 1 and not result.rejected


async def test_rejects_nondeterministic_dry_run():
    gate = CheckProposalGate(FakeRunner([0, 1]))
    result = await gate.filter([_shell(["python", "x.py"])], existing_dedup_keys=set())
    assert not result.accepted and result.rejected[0][1] == "nondeterministic_dry_run"


async def test_rejects_non_allowlisted_argv():
    gate = CheckProposalGate(FakeRunner([0, 0]))
    result = await gate.filter([_shell(["rm", "-rf", "x"])], existing_dedup_keys=set())
    assert result.rejected[0][1] == "argv_not_allowlisted"


async def test_rejects_duplicate_and_unsafe_path():
    gate = CheckProposalGate(FakeRunner([0, 0]))
    p = _shell(["python", "x.py"])
    dup = await gate.filter([p], existing_dedup_keys={p.dedup_key()})
    assert dup.rejected[0][1] == "duplicate"
    esc = await gate.filter([_file("../outside.py")], existing_dedup_keys=set())
    assert esc.rejected[0][1] == "unsafe_path"


async def test_file_exists_needs_no_dry_run():
    runner = FakeRunner([0])
    gate = CheckProposalGate(runner)
    result = await gate.filter([_file("mini_regex.py")], existing_dedup_keys=set())
    assert len(result.accepted) == 1 and runner.calls == []
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/unit/test_check_proposal_gate.py -q` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# src/hungerloop/services/check_proposal_gate.py
"""Deterministic pre-injection gate for CheckProposal items (I-7 safe).

Order of rejection reasons: duplicate -> shape -> allowlist/path ->
nondeterministic_dry_run. FILE_EXISTS proposals skip dry-run (pure
filesystem predicate); SHELL_EXIT_ZERO runs twice and both runs must
return the same exit code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal

DEFAULT_ARGV_ALLOWLIST: tuple[str, ...] = ("python",)


class DryRunner(Protocol):
    async def run(self, argv: list[str], timeout_seconds: int) -> int: ...


@dataclass
class GateResult:
    accepted: list[CheckProposal] = field(default_factory=list)
    rejected: list[tuple[CheckProposal, str]] = field(default_factory=list)


def _is_safe_relative_path(raw: str) -> bool:
    if not raw or "\x00" in raw:
        return False
    for cls in (PurePosixPath, PureWindowsPath):
        p = cls(raw)
        if p.is_absolute() or ".." in p.parts:
            return False
    return True


class CheckProposalGate:
    def __init__(
        self,
        dry_runner: DryRunner,
        *,
        argv_allowlist: tuple[str, ...] = DEFAULT_ARGV_ALLOWLIST,
        timeout_seconds: int = 120,
    ) -> None:
        self.dry_runner = dry_runner
        self.argv_allowlist = tuple(a.lower() for a in argv_allowlist)
        self.timeout_seconds = timeout_seconds

    async def filter(
        self,
        proposals: list[CheckProposal],
        *,
        existing_dedup_keys: set[str],
    ) -> GateResult:
        result = GateResult()
        seen = set(existing_dedup_keys)
        for proposal in proposals:
            key = proposal.dedup_key()
            if key in seen:
                result.rejected.append((proposal, "duplicate"))
                continue
            reason = await self._check_one(proposal)
            if reason is None:
                seen.add(key)
                result.accepted.append(proposal)
            else:
                result.rejected.append((proposal, reason))
        return result

    async def _check_one(self, proposal: CheckProposal) -> str | None:
        if proposal.check_type is AcceptanceCheckType.FILE_EXISTS:
            path = proposal.params.get("path")
            if not isinstance(path, str) or not _is_safe_relative_path(path):
                return "unsafe_path"
            return None
        argv = proposal.params.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) for part in argv
        ):
            return "invalid_argv"
        head = str(argv[0]).strip().lower()
        if head not in self.argv_allowlist:
            return "argv_not_allowlisted"
        first = await self.dry_runner.run(list(argv), self.timeout_seconds)
        second = await self.dry_runner.run(list(argv), self.timeout_seconds)
        if first != second:
            return "nondeterministic_dry_run"
        return None
```

- [ ] **Step 4: Run** — 5 passed; then `mypy --strict src/hungerloop/services/check_proposal_gate.py src/hungerloop/models/synthesis.py` → clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: add deterministic CheckProposalGate (allowlist, path safety, dry-run x2)"`

### Task 4: `RefinementCompiler.compile_spec_coverage` (ledger injection)

**Files:**
- Modify: `src/hungerloop/services/refinement_compiler.py` (class `RefinementCompiler`, line ~73)
- Test: `tests/unit/test_compile_spec_coverage.py`

**Interfaces:**
- Consumes: `CheckProposal` (Task 1).
- Produces: `def compile_spec_coverage(self, task_id: str, proposals: list[CheckProposal], *, generated_by: str, tier: int = 1, max_new_items: int = 20) -> list[str]` returning new hunger item ids (`H-SYN-001` style). This is the ONLY ledger write path for proposals (CI rule).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_compile_spec_coverage.py
from __future__ import annotations

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler


def _seed(repo: InMemoryRepository, task_id: str) -> None:
    _, ledger = RequirementCompiler(repo).compile(
        task_id, "goal",
        hints={"core_acceptance_checks": [{
            "check_type": "file_exists", "params": {"path": "m.py"},
            "description": "deliverable",
        }]},
    )
    repo.save_hunger_ledger(task_id, ledger)


def _prop(path: str) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS, params={"path": path},
        description=f"exists {path}", source_quote="spec says so", proposed_by="p",
    )


def test_injects_tiered_items_with_provenance():
    repo = InMemoryRepository(); _seed(repo, "t1")
    ids = RefinementCompiler(repo).compile_spec_coverage(
        "t1", [_prop("a.py"), _prop("b.py")], generated_by="spec_check_synthesizer",
    )
    ledger = repo.get_hunger_ledger("t1")
    items = {i.id: i for i in ledger.items}
    assert ids == ["H-SYN-001", "H-SYN-002"]
    assert items["H-SYN-001"].refinement_tier == 1
    assert items["H-SYN-001"].refinement_kind == "spec_coverage"
    assert items["H-SYN-001"].generated_by == "spec_check_synthesizer"
    assert items["H-SYN-001"].acceptance_checks[0].params == {"path": "a.py"}


def test_dedup_against_existing_ledger_and_cap():
    repo = InMemoryRepository(); _seed(repo, "t2")
    compiler = RefinementCompiler(repo)
    compiler.compile_spec_coverage("t2", [_prop("a.py")], generated_by="x")
    again = compiler.compile_spec_coverage(
        "t2", [_prop("a.py"), _prop("c.py"), _prop("d.py")],
        generated_by="x", max_new_items=1,
    )
    assert again == ["H-SYN-002"]  # a.py deduped, cap allows only one more
```

- [ ] **Step 2: Run, verify failure** — AttributeError: no `compile_spec_coverage`.

- [ ] **Step 3: Implement** — add to `RefinementCompiler` (import `CheckProposal`, `AcceptanceCheck`, `HungerItem` at top; they are already partially imported in this module — extend the imports):

```python
    def compile_spec_coverage(
        self,
        task_id: str,
        proposals: list[CheckProposal],
        *,
        generated_by: str,
        tier: int = 1,
        max_new_items: int = 20,
    ) -> list[str]:
        """Inject gated proposals as spec_coverage hunger items (compiler-owned write)."""
        ledger = self.repo.get_hunger_ledger(task_id)
        existing_keys = {
            CheckProposal(
                check_type=check.check_type, params=dict(check.params),
                description=check.description, source_quote="existing",
                proposed_by="existing",
            ).dedup_key()
            for item in ledger.items
            for check in item.acceptance_checks
            if check.check_type.value in ("shell_exit_zero", "file_exists")
        }
        syn_count = sum(1 for item in ledger.items if item.id.startswith("H-SYN-"))
        new_ids: list[str] = []
        for proposal in proposals:
            if len(new_ids) >= max_new_items:
                break
            key = proposal.dedup_key()
            if key in existing_keys:
                continue
            existing_keys.add(key)
            syn_count += 1
            item = HungerItem(
                id=f"H-SYN-{syn_count:03d}",
                title=(proposal.description or "Spec-coverage check")[:80],
                refinement_tier=tier,
                refinement_kind="spec_coverage",
                generated_by=generated_by,
                acceptance_checks=[
                    AcceptanceCheck(
                        check_type=proposal.check_type,
                        params=dict(proposal.params),
                        description=proposal.description or proposal.source_quote,
                    )
                ],
            )
            ledger.items.append(item)
            new_ids.append(item.id)
        if new_ids:
            self.repo.save_hunger_ledger(task_id, ledger)
        return new_ids
```

- [ ] **Step 4: Run** — task tests pass; `python -m pytest tests/unit/test_refinement_compiler*.py -q` no regressions; mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: compile_spec_coverage injects gated proposals via RefinementCompiler"`

### Task 5: `SpecCheckSynthesizer` + orchestrator wiring

**Files:**
- Create: `src/hungerloop/services/spec_check_synthesizer.py`
- Modify: `src/hungerloop/services/loop_orchestrator.py` (post-commit hook), `src/hungerloop/cli/orchestrator_factory.py` (wiring)
- Test: `tests/unit/test_spec_check_synthesizer.py`

**Interfaces:**
- Consumes: `CheckProposalGate.filter` (Task 3), `RefinementCompiler.compile_spec_coverage` (Task 4), `CostGuard.assert_within_budget(task_id)` (existing, see `worker_runtime.py:100`).
- Produces: `class CompletionClient(Protocol)` with `async def complete(self, prompt: str) -> str`; `class SpecCheckSynthesizer` with `async def synthesize(self, *, task_id: str, spec_text: str, covered_digest: str) -> list[CheckProposal]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_spec_check_synthesizer.py
from __future__ import annotations

import json

from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate
from hungerloop.services.spec_check_synthesizer import SpecCheckSynthesizer


class FakeClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.payload


class OkRunner:
    async def run(self, argv: list[str], timeout_seconds: int) -> int:
        return 1


class NoBudgetGuard:
    def __init__(self) -> None:
        self.calls = 0

    def assert_within_budget(self, task_id: str) -> None:
        self.calls += 1


def _payload() -> str:
    return json.dumps([
        {"check_type": "shell_exit_zero",
         "params": {"argv": ["python", "-m", "pytest", "j.py::test_nested"]},
         "description": "nested group numbering",
         "source_quote": "(a(b)c) has group 1 = 'abc'"},
        {"check_type": "llm_judge", "params": {},
         "description": "bad", "source_quote": "q"},
    ])


async def test_parses_gates_and_wraps_cost_guard():
    repo = InMemoryRepository()
    guard = NoBudgetGuard()
    synth = SpecCheckSynthesizer(
        client=FakeClient(_payload()),
        gate=CheckProposalGate(OkRunner()),
        cost_guard=guard, repo=repo,
    )
    accepted = synth_result = await synth.synthesize(
        task_id="t1", spec_text="THE SPEC", covered_digest="covered: none",
    )
    assert [p.description for p in synth_result] == ["nested group numbering"]
    assert isinstance(accepted[0], CheckProposal)
    assert guard.calls == 2  # before and after the LLM call (I-8)
    assert "THE SPEC" in synth._last_prompt_for_test()


async def test_tolerates_fenced_json_and_bad_items():
    repo = InMemoryRepository()
    fenced = "```json\n" + _payload() + "\n```"
    synth = SpecCheckSynthesizer(
        client=FakeClient(fenced), gate=CheckProposalGate(OkRunner()),
        cost_guard=NoBudgetGuard(), repo=repo,
    )
    accepted = await synth.synthesize(task_id="t", spec_text="s", covered_digest="")
    assert len(accepted) == 1


async def test_garbage_response_returns_empty():
    synth = SpecCheckSynthesizer(
        client=FakeClient("not json at all"), gate=CheckProposalGate(OkRunner()),
        cost_guard=NoBudgetGuard(), repo=InMemoryRepository(),
    )
    assert await synth.synthesize(task_id="t", spec_text="s", covered_digest="") == []
```

- [ ] **Step 2: Run, verify failure** — ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# src/hungerloop/services/spec_check_synthesizer.py
"""LLM-assisted spec-to-check synthesis (plan-time + post-commit).

Lives OUTSIDE services/validators/ by contract: validation stays
deterministic; this service only proposes checks, and every proposal
must pass CheckProposalGate before RefinementCompiler injects it.
"""
from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.check_proposal_gate import CheckProposalGate


class CompletionClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


class CostGuardLike(Protocol):
    def assert_within_budget(self, task_id: str) -> None: ...


_PROMPT_TEMPLATE = """You extract deterministic acceptance checks from a software spec.
Return ONLY a JSON array. Each element:
{{"check_type": "shell_exit_zero"|"file_exists",
  "params": {{"argv": [...]}} or {{"path": "relative/path"}},
  "description": "short behavior name",
  "source_quote": "verbatim spec excerpt motivating this check"}}
Rules: argv[0] must be "python"; propose ONLY behaviors stated in the
spec that are NOT already covered; prefer single-test pytest commands.

ALREADY COVERED:
{covered_digest}

SPEC:
{spec_text}
"""


class SpecCheckSynthesizer:
    def __init__(
        self,
        *,
        client: CompletionClient,
        gate: CheckProposalGate,
        cost_guard: CostGuardLike,
        repo: RepositoryProtocol,
        proposed_by: str = "spec_check_synthesizer",
    ) -> None:
        self.client = client
        self.gate = gate
        self.cost_guard = cost_guard
        self.repo = repo
        self.proposed_by = proposed_by
        self._last_prompt: str = ""

    def _last_prompt_for_test(self) -> str:
        return self._last_prompt

    async def synthesize(
        self, *, task_id: str, spec_text: str, covered_digest: str
    ) -> list[CheckProposal]:
        prompt = _PROMPT_TEMPLATE.format(
            covered_digest=covered_digest or "(none)", spec_text=spec_text
        )
        self._last_prompt = prompt
        self.cost_guard.assert_within_budget(task_id)
        raw = await self.client.complete(prompt)
        self.cost_guard.assert_within_budget(task_id)
        proposals = self._parse(task_id, raw)
        result = await self.gate.filter(proposals, existing_dedup_keys=set())
        for proposal, reason in result.rejected:
            self.repo.append_event(
                "SYNTH_CHECK_REJECTED",
                {"reason": reason, "source_quote": proposal.source_quote,
                 "description": proposal.description},
                task_id=task_id,
            )
        return result.accepted

    def _parse(self, task_id: str, raw: str) -> list[CheckProposal]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.repo.append_event(
                "SYNTH_CHECK_REJECTED",
                {"reason": "unparseable_response", "description": raw[:200]},
                task_id=task_id,
            )
            return []
        if not isinstance(data, list):
            return []
        proposals: list[CheckProposal] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                proposals.append(
                    CheckProposal(proposed_by=self.proposed_by, **entry)
                )
            except (ValidationError, TypeError):
                continue
        return proposals
```

- [ ] **Step 4: Run** — 3 passed; mypy clean.

- [ ] **Step 5: Wire into orchestrator (post-commit) and factory.** In `src/hungerloop/services/loop_orchestrator.py`, locate the successful-commit branch (search for the call site of `memory_manager.propose_from_loop`, around line 74; the committed branch is where `decision["committed"]` is true). Insert AFTER commit success:

```python
        if (
            self.spec_check_synthesizer is not None
            and policy.synthesis_enabled
        ):
            syn_total = sum(
                1 for it in self.repo.get_hunger_ledger(task_id).items
                if it.id.startswith("H-SYN-")
            )
            if syn_total < policy.synthesis_max_total_items:
                accepted = await self.spec_check_synthesizer.synthesize(
                    task_id=task_id,
                    spec_text=self._mission_spec_text(task_id),
                    covered_digest=self._covered_checks_digest(task_id),
                )
                if accepted:
                    self.refinement_compiler.compile_spec_coverage(
                        task_id, accepted,
                        generated_by="spec_check_synthesizer",
                        max_new_items=min(
                            budget.max_new_items_per_loop,
                            policy.synthesis_max_total_items - syn_total,
                        ),
                    )
```

Add to the orchestrator: constructor param `spec_check_synthesizer: SpecCheckSynthesizer | None = None` (store on self); helper `_mission_spec_text(task_id)` returns `"\n\n".join(feature descriptions)` from `self.repo.get_mission(task_id)` (empty string when no mission); helper `_covered_checks_digest(task_id)` returns newline-joined `f"{key}: {check.description}"` for every item/check in the ledger, clipped to 4000 chars. `policy` is already available in the loop (`repo.get_hunger_policy(task_id)` is how other services fetch it). In `src/hungerloop/cli/orchestrator_factory.py`, construct the synthesizer only when `policy.synthesis_enabled`: adapt the existing `ModelClient` to `CompletionClient` (a small `class _ModelCompletionAdapter` in the factory calling the client's single-completion method — read `services/openai_model_client.py` for the exact method name and reuse how `ExecutionWorker` invokes it) and adapt `SandboxRunner` to `DryRunner` (`class _SandboxDryRunner` calling `SandboxRunner`'s run method with the task's candidate workspace cwd — read `services/sandbox_runner.py` for the exact signature). Plan-time injection: in the mission-run entry (`cli/mission_cmd.py` run path, before loop 1 starts), when `policy.synthesis_enabled`, call `synthesize` once with the same spec text and inject with `tier=policy.synthesis_plan_time_tier`. Add unit test `tests/unit/test_orchestrator_synthesis_hook.py` that stubs the synthesizer with a fake returning one proposal and asserts a `H-SYN-001` item exists after a committed loop (mirror the stub pattern of `tests/unit/test_loop_orchestrator.py`).

- [ ] **Step 6: Run full gate** — `python -m pytest tests/unit/ -q` (no new failures), `mypy --strict src/`, `ruff check src/ tests/`.

- [ ] **Step 7: Commit** — `git commit -m "feat: SpecCheckSynthesizer with post-commit and plan-time injection (flag-gated)"`

---

## Phase 3a — Discovery credit

### Task 6: Worker check proposals via handoff

**Files:**
- Modify: `src/hungerloop/models/worker.py` (`HandoffItem`), `src/hungerloop/models/handoff.py` (`HandoffProcessingResult`), `src/hungerloop/services/handoff_processor.py`
- Test: `tests/unit/test_handoff_proposed_checks.py`

**Interfaces:**
- Consumes: `CheckProposalGate` (Task 3), `RefinementCompiler.compile_spec_coverage` (Task 4).
- Produces: `HandoffItem.proposed_checks: list[CheckProposal]` (default empty); `HandoffProcessor.__init__` gains `proposal_gate: CheckProposalGate | None = None, refinement_compiler: RefinementCompiler | None = None`; `HandoffProcessingResult.accepted_proposal_count: int = 0`; injected items get `generated_by=f"worker:{agent_id}"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_handoff_proposed_checks.py
from __future__ import annotations

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler


class OkRunner:
    async def run(self, argv: list[str], timeout_seconds: int) -> int:
        return 1


async def test_worker_proposal_injected_with_worker_provenance():
    repo = InMemoryRepository()
    _, ledger = RequirementCompiler(repo).compile(
        "t1", "goal",
        hints={"core_acceptance_checks": [{
            "check_type": "file_exists", "params": {"path": "m.py"},
            "description": "core"}]},
    )
    repo.save_hunger_ledger("t1", ledger)
    processor = HandoffProcessor(
        repo,
        proposal_gate=CheckProposalGate(OkRunner()),
        refinement_compiler=RefinementCompiler(repo),
    )
    handoff = WorkerHandoff(
        agent_id="execution_worker_v1", task_id="t1", loop_id=2,
        handoff_items=[HandoffItem(
            item_type="discovered_issue",
            summary="nested groups untested",
            detail="spec example (a(b)c) is not covered by any check",
            proposed_checks=[CheckProposal(
                check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                params={"argv": ["python", "-m", "pytest", "j.py::test_nested"]},
                description="nested group numbering",
                source_quote="(a(b)c) has group 1 = 'abc'",
                proposed_by="worker",
            )],
        )],
    )
    result = processor.process_handoffs(
        "t1", 2, [handoff], mission=None, budget=BudgetAllocation(),
    )
    items = {i.id: i for i in repo.get_hunger_ledger("t1").items}
    assert result.accepted_proposal_count == 1
    assert items["H-SYN-001"].generated_by == "worker:execution_worker_v1"
```

Note: `process_handoffs` is sync today; the gate is async. Make the proposal-routing helper async and have `process_handoffs` call it via a small internal `asyncio.get_event_loop()`-free pattern: convert `process_handoffs` itself to `async def` ONLY if its two production callers (`loop_orchestrator.py`, `validators/scrutiny_validator.py`) already `await` in scope — check both call sites first. If they are sync, instead run the gate eagerly BEFORE `process_handoffs` in the orchestrator (which is async) and pass pre-gated proposals through; in that case `HandoffProcessor` receives `pre_gated: dict[str, list[CheckProposal]]` keyed by handoff_id. Pick whichever matches the call sites; the test above then adapts to construct accordingly. Document the choice in the commit message.

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** — `HandoffItem` gains `proposed_checks: list[CheckProposal] = Field(default_factory=list)` (import inside `models/worker.py`); `HandoffProcessingResult` gains `accepted_proposal_count: int = 0`; in `handoff_processor.py`'s `discovered_issue` branch (after the existing `compile_discovered_facts` block, `handoff_processor.py:85-113`), route `item.proposed_checks` through the gate and `compile_spec_coverage(task_id, accepted, generated_by=f"worker:{handoff.agent_id}", max_new_items=cap - discovered_issue_count)`, incrementing `accepted_proposal_count` on the result.

- [ ] **Step 4: Run** — new test passes; `python -m pytest tests/unit/test_handoff_processor.py tests/unit/test_context_builder_handoff.py -q` no regressions.

- [ ] **Step 5: Commit** — `git commit -m "feat: workers propose deterministic checks via discovered_issue handoffs"`

### Task 7: `DISCOVERY_CREDIT` emission + bounded streak reset + stop-report summary

**Files:**
- Modify: `src/hungerloop/services/commit_manager.py` (`apply`, inside the commit transaction after the `save_accepted_check` loop at lines 109-119), `src/hungerloop/services/loop_orchestrator.py` (after handoff processing), `src/hungerloop/services/stop_report_builder.py`, `src/hungerloop/repository/protocol.py` + both repos (`list_events`)
- Test: `tests/unit/test_discovery_credit.py`

**Interfaces:**
- Produces: repo method `list_events(task_id: str, *, event_type: str | None = None) -> list[dict[str, object]]` (rows contain at least `event_type`, `payload`, `loop_id`); event `"DISCOVERY_CREDIT"` with payload `{"proposer": item.generated_by, "check_key": key, "loop_id": candidate.loop_id}`; StopReport payload gains `discovery_credits: dict[str, int]` (proposer -> count).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_credit.py
from __future__ import annotations

# Construct a CommitManager exactly the way tests/unit/test_commit_manager.py
# does (reuse its fixtures/helpers for candidate + validation report), with a
# ledger where item "H-SYN-001" has generated_by="worker:w1" and the report's
# newly_passed_check_keys == ["H-SYN-001:0"]. After apply():


def test_commit_emits_discovery_credit(commit_env):  # reuse existing fixture pattern
    repo, manager, candidate, report = commit_env(
        newly_passed=["H-SYN-001:0"], generated_by="worker:w1",
    )
    decision = manager.apply(candidate, report)
    assert decision["committed"] is True
    events = repo.list_events(candidate.task_id, event_type="DISCOVERY_CREDIT")
    assert len(events) == 1
    assert events[0]["payload"]["proposer"] == "worker:w1"


def test_no_credit_for_synthesizer_items(commit_env):
    repo, manager, candidate, report = commit_env(
        newly_passed=["H-SYN-001:0"], generated_by="spec_check_synthesizer",
    )
    manager.apply(candidate, report)
    assert repo.list_events(candidate.task_id, event_type="DISCOVERY_CREDIT") == []
```

Write the `commit_env` factory fixture in this test file by copying the candidate/report construction from `tests/unit/test_commit_manager.py` (do not import its internals; duplicate the minimal setup).

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** —
  1. `protocol.py`: add `list_events`; `in_memory_repo.py`: filter its event list; `sqlite_repo.py`: `SELECT event_type, loop_id, payload_json FROM events WHERE task_id=? AND (? IS NULL OR event_type=?)`, decode `payload_json` into `payload`.
  2. `commit_manager.py` inside the transaction, after the `for check_key in report.newly_passed_check_keys:` loop:

```python
                    ledger = self.repo.get_hunger_ledger(candidate.task_id)
                    items_by_id = {item.id: item for item in ledger.items}
                    for check_key in report.newly_passed_check_keys:
                        owner = items_by_id.get(check_key.split(":", 1)[0])
                        if owner is not None and (owner.generated_by or "").startswith("worker:"):
                            self.repo.append_event(
                                "DISCOVERY_CREDIT",
                                {"proposer": owner.generated_by,
                                 "check_key": check_key,
                                 "loop_id": candidate.loop_id},
                                task_id=candidate.task_id,
                                loop_id=candidate.loop_id,
                            )
```

  3. `loop_orchestrator.py`, immediately after the `process_handoffs` result is obtained: `if result.accepted_proposal_count > 0: self.repo.reset_no_progress_streak(task_id)` (at most once per loop by construction).
  4. `stop_report_builder.py`: read the file; where the payload dict is assembled, add `"discovery_credits": credits` computed as a `dict[str, int]` counter over `repo.list_events(task_id, event_type="DISCOVERY_CREDIT")` payload `proposer` values.

- [ ] **Step 4: Run** — task tests + `tests/unit/test_commit_manager.py` + `tests/unit/test_stop_report*.py -q` all green; mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: DISCOVERY_CREDIT events, bounded streak reset, stop-report credit summary"`

---

## Phase 2 — Refactor transactions + commit selection

### Task 8: `RefactorTransaction` model + repository + migration

**Files:**
- Create: `src/hungerloop/models/refactor.py`
- Modify: `src/hungerloop/repository/protocol.py`, `in_memory_repo.py`, `sqlite_repo.py`; add next migration file under `src/hungerloop/repository/migrations/` (inspect the directory: files are numbered `NNN_*.sql` or versioned Python — follow the existing pattern exactly and bump `LATEST_VERSION`)
- Test: `tests/unit/test_refactor_transaction_repo.py`

**Interfaces:**
- Produces: `RefactorTransaction(transaction_id, task_id, declared_regression_keys, rationale, opened_at_loop, deadline_loops, baseline_best_state_json, baseline_accepted_check_keys, status)` with `status: Literal["open","closed_success","rolled_back"]="open"`; repo methods `save_refactor_transaction(txn)`, `get_open_refactor_transaction(task_id) -> RefactorTransaction | None`, `update_refactor_transaction_status(transaction_id, status)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_refactor_transaction_repo.py
from __future__ import annotations

import pytest

from hungerloop.models.refactor import RefactorTransaction
from hungerloop.repository.in_memory_repo import InMemoryRepository


def _txn(task_id: str = "t1") -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id="TXN-1", task_id=task_id,
        declared_regression_keys=["H-001:0"], rationale="restructure parser",
        opened_at_loop=3, deadline_loops=3,
        baseline_best_state_json="{}", baseline_accepted_check_keys=["H-001:0"],
    )


@pytest.fixture(params=["memory", "sqlite"])
def repo(request, tmp_path):
    if request.param == "memory":
        return InMemoryRepository()
    from hungerloop.repository.sqlite_repo import SQLiteRepository
    return SQLiteRepository(tmp_path / "t.sqlite")  # match ctor used in existing sqlite tests


def test_round_trip_and_status_update(repo):
    repo.save_refactor_transaction(_txn())
    open_txn = repo.get_open_refactor_transaction("t1")
    assert open_txn is not None and open_txn.transaction_id == "TXN-1"
    repo.update_refactor_transaction_status("TXN-1", "rolled_back")
    assert repo.get_open_refactor_transaction("t1") is None
```

(Adapt the `SQLiteRepository` constructor call to match `tests/unit/test_sqlite_repo*.py` — read one before writing this fixture.)

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** — model:

```python
# src/hungerloop/models/refactor.py
"""RefactorTransaction — bounded non-monotonic commit window (ADR-010)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TransactionStatus = Literal["open", "closed_success", "rolled_back"]


class RefactorTransaction(BaseModel):
    transaction_id: str
    task_id: str
    declared_regression_keys: list[str]
    rationale: str = ""
    opened_at_loop: int
    deadline_loops: int = 3
    baseline_best_state_json: str
    baseline_accepted_check_keys: list[str] = Field(default_factory=list)
    status: TransactionStatus = "open"
```

Migration SQL (new versioned file, following the directory's existing naming; forward-only, wrapped exactly like the previous migration):

```sql
CREATE TABLE IF NOT EXISTS refactor_transactions (
    transaction_id TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    opened_at_loop INTEGER NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refactor_txn_task_status
    ON refactor_transactions(task_id, status);
```

Repo methods store `payload_json = txn.model_dump_json()`; `get_open_refactor_transaction` selects `WHERE task_id=? AND status='open'` and `RefactorTransaction.model_validate_json(payload_json)`; `update_refactor_transaction_status` updates both the column and the JSON payload's `status`.

- [ ] **Step 4: Run** — both params pass; run the whole `tests/unit/test_sqlite_repo*.py` and any migration tests to confirm `user_version` bump is accepted.

- [ ] **Step 5: Commit** — `git commit -m "feat: RefactorTransaction model, repo methods, sqlite migration"`

### Task 9: `select_commit_candidate` (fan-out-ready, deterministic, no score)

**Files:**
- Create: `src/hungerloop/services/commit_selection.py`
- Test: `tests/unit/test_commit_selection.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) CandidateEvaluation(candidate_id: str, gate_passed: bool, newly_passed_count: int, failing_count: int)`; `def select_commit_candidate(evals: Sequence[CandidateEvaluation]) -> CandidateEvaluation | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_commit_selection.py
from __future__ import annotations

from hungerloop.services.commit_selection import CandidateEvaluation, select_commit_candidate


def _e(cid: str, gate: bool, newly: int, failing: int) -> CandidateEvaluation:
    return CandidateEvaluation(cid, gate, newly, failing)


def test_none_when_no_gate_passer():
    assert select_commit_candidate([_e("a", False, 9, 0)]) is None


def test_orders_by_newly_then_failing_then_id():
    winner = select_commit_candidate([
        _e("b", True, 3, 2), _e("a", True, 3, 2), _e("c", True, 3, 1),
        _e("d", True, 5, 9), _e("x", False, 99, 0),
    ])
    assert winner is not None and winner.candidate_id == "d"
    tie = select_commit_candidate([_e("b", True, 3, 1), _e("a", True, 3, 1)])
    assert tie is not None and tie.candidate_id == "a"


def test_empty_input():
    assert select_commit_candidate([]) is None
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement**

```python
# src/hungerloop/services/commit_selection.py
"""Deterministic commit-candidate selection (fan-out ready).

I-3: no score participates. Ordering: gate-passing first, then most
newly-passed checks, then fewest failing checks, then candidate_id
lexicographic for a stable total order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    gate_passed: bool
    newly_passed_count: int
    failing_count: int


def select_commit_candidate(
    evals: Sequence[CandidateEvaluation],
) -> CandidateEvaluation | None:
    passers = [e for e in evals if e.gate_passed]
    if not passers:
        return None
    return min(
        passers,
        key=lambda e: (-e.newly_passed_count, e.failing_count, e.candidate_id),
    )
```

- [ ] **Step 4: Run** — 3 passed; mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: deterministic select_commit_candidate for future fan-out"`

### Task 10: Transaction-aware commit gate (I-3 amendment) + ADR + CLAUDE.md

**Files:**
- Modify: `src/hungerloop/services/commit_manager.py` (`apply` signature + `_can_commit`, lines 58-64 and 195-205)
- Create: `docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md`
- Modify: `CLAUDE.md` (I-3 bullet)
- Test: `tests/unit/test_commit_manager_txn.py`

**Interfaces:**
- Consumes: `RefactorTransaction` (Task 8).
- Produces: `CommitManager.apply(candidate, validation, *, completed_feature_ids=None, open_transaction: RefactorTransaction | None = None)`; `_can_commit(report, open_transaction)` tolerating `regressed ⊆ declared_regression_keys`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_commit_manager_txn.py
from __future__ import annotations

# Reuse the same commit_env construction pattern as Task 7's test file:
# a report with newly_passed=["H-002:0"], regressed=["H-001:0"].


def test_regression_inside_R_tolerated_when_txn_open(commit_env, open_txn):
    repo, manager, candidate, report = commit_env(
        newly_passed=["H-002:0"], regressed=["H-001:0"],
    )
    txn = open_txn(declared=["H-001:0"])
    decision = manager.apply(candidate, report, open_transaction=txn)
    assert decision["committed"] is True


def test_regression_outside_R_still_rejected(commit_env, open_txn):
    repo, manager, candidate, report = commit_env(
        newly_passed=["H-002:0"], regressed=["H-003:0"],
    )
    decision = manager.apply(
        candidate, report, open_transaction=open_txn(declared=["H-001:0"]),
    )
    assert decision["committed"] is False
    assert decision["reason"] == "regressed_checks_detected"


def test_no_txn_keeps_v06_gate(commit_env):
    repo, manager, candidate, report = commit_env(
        newly_passed=["H-002:0"], regressed=["H-001:0"],
    )
    assert manager.apply(candidate, report)["committed"] is False
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** — change `_can_commit`:

```python
    def _can_commit(
        self,
        report: ValidationReport,
        open_transaction: RefactorTransaction | None = None,
    ) -> bool:
        """I-3 commit conditions, with the ADR-010 refactor-transaction exception."""
        if report.verdict not in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}:
            return False
        if not report.newly_passed_check_keys:
            return False
        regressed = set(report.regressed_check_keys)
        if open_transaction is not None:
            regressed -= set(open_transaction.declared_regression_keys)
        if regressed:
            return False
        if report.missing_evidence:
            return False
        return True
```

Thread `open_transaction` through `apply(...)` (keyword-only, default `None`; pass to `_can_commit`). Also add to `apply`'s reject path: `_reject_reason(report)` unchanged. Write `docs/architecture/v0.7/adr/ADR-010-refactor-transactions.md` (Status: Accepted; Context: single-lineage greedy hill-climbing cannot restructure; Decision: bounded regression window R with K-loop deadline, snapshot rollback, flag-gated default-off; Consequences: I-3 text amended, stagnation exempts R while open). Amend the CLAUDE.md I-3 bullet by appending: `Exception (ADR-010): while a RefactorTransaction is open, regressions within its declared_regression_keys are tolerated; settlement requires re-passing R plus net-new accepted checks, else automatic rollback to the transaction baseline.`

- [ ] **Step 4: Run** — new tests + `tests/unit/test_commit_manager.py -q` green.

- [ ] **Step 5: Commit** — `git commit -m "feat: transaction-aware commit gate with ADR-010 I-3 amendment"`

### Task 11: `RefactorTransactionManager` (open/settle/rollback) + handoff + orchestrator wiring

**Files:**
- Create: `src/hungerloop/services/refactor_transaction_manager.py`
- Modify: `src/hungerloop/models/worker.py` (`HandoffItemType` += `"refactor_proposal"`), `src/hungerloop/services/handoff_processor.py` (route), `src/hungerloop/services/stagnation_detector.py` (exemption), `src/hungerloop/services/loop_orchestrator.py` (settle hook + pass `open_transaction` into `CommitManager.apply`)
- Test: `tests/unit/test_refactor_transaction_manager.py`, `tests/integration/test_refactor_rollback_sqlite.py`

**Interfaces:**
- Consumes: repo txn methods (Task 8), `WorkspaceManager.best_files_dir(task_id)` (existing, see `commit_manager.py:126`), `BestState` (existing).
- Produces:
  - `RefactorTransactionManager(repo, workspace_manager)` with:
    - `open(task_id: str, loop_id: int, declared_regression_keys: list[str], rationale: str) -> RefactorTransaction | str` (returns reject-reason string on refusal: `"disabled"`, `"txn_already_open"`, `"keys_not_accepted"`, `"too_many_keys"`),
    - `settle_if_due(task_id: str, loop_id: int, *, force: bool = False) -> str | None` (returns `"closed_success"`, `"rolled_back"`, or `None` when not due).
  - `StagnationDetector.update(..., regression_exempt_item_ids: set[str] | None = None)` — exempted item ids never increment `consecutive_failure_count`.

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/unit/test_refactor_transaction_manager.py
from __future__ import annotations

# Fixtures: an InMemoryRepository seeded with a BestState whose
# accepted_check_keys == ["H-001:0", "H-001:1"], policy with
# refactor_transactions_enabled=True, and a temp workspace where
# workspace_manager.best_files_dir(task_id) contains one file "m.py".
# Build WorkspaceManager the same way tests/unit/test_commit_manager.py does.


def test_open_rules(mgr_env):
    mgr, repo = mgr_env(enabled=True)
    assert mgr.open("t1", 3, ["H-009:0"], "r") == "keys_not_accepted"
    assert mgr.open("t1", 3, ["H-001:0"] * 6, "r") == "too_many_keys"
    txn = mgr.open("t1", 3, ["H-001:0"], "split parser")
    assert not isinstance(txn, str) and txn.status == "open"
    assert mgr.open("t1", 3, ["H-001:1"], "again") == "txn_already_open"


def test_open_respects_disabled_flag(mgr_env):
    mgr, _ = mgr_env(enabled=False)
    assert mgr.open("t1", 3, ["H-001:0"], "r") == "disabled"


def test_settle_success_and_rollback(mgr_env):
    mgr, repo = mgr_env(enabled=True)
    txn = mgr.open("t1", 3, ["H-001:0"], "r")
    # not due yet
    assert mgr.settle_if_due("t1", 4) is None
    # due at opened_at_loop + deadline_loops (3+3): R re-passed + net new
    repo.set_accepted_for_test("t1", ["H-001:0", "H-001:1", "H-002:0"])
    assert mgr.settle_if_due("t1", 6) == "closed_success"
    # second txn that fails settlement rolls back and restores the snapshot
    txn2 = mgr.open("t1", 7, ["H-001:0"], "r2")
    repo.set_accepted_for_test("t1", ["H-001:1"])  # R not re-passed
    assert mgr.settle_if_due("t1", 10) == "rolled_back"
    assert repo.get_best_state("t1").accepted_check_keys == [
        "H-001:0", "H-001:1", "H-002:0",
    ]
```

(`set_accepted_for_test` = write a `BestState` row directly via `repo.save_best_state`; write a tiny helper in the test, not on the repo.)

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement**

```python
# src/hungerloop/services/refactor_transaction_manager.py
"""Open, settle, and roll back RefactorTransactions (ADR-010)."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from hungerloop.models.blackboard import BestState
from hungerloop.models.refactor import RefactorTransaction
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.workspace_manager import WorkspaceManager


class RefactorTransactionManager:
    def __init__(self, repo: RepositoryProtocol, workspace_manager: WorkspaceManager) -> None:
        self.repo = repo
        self.workspace_manager = workspace_manager

    def _snapshot_dir(self, task_id: str, transaction_id: str) -> Path:
        best_dir = self.workspace_manager.best_files_dir(task_id)
        return best_dir.parent / f".txn_{transaction_id}"

    def open(
        self, task_id: str, loop_id: int,
        declared_regression_keys: list[str], rationale: str,
    ) -> RefactorTransaction | str:
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.refactor_transactions_enabled:
            return "disabled"
        if self.repo.get_open_refactor_transaction(task_id) is not None:
            return "txn_already_open"
        best = self.repo.get_best_state(task_id)
        accepted = set(best.accepted_check_keys) if best else set()
        keys = list(dict.fromkeys(declared_regression_keys))
        if not keys or not set(keys) <= accepted:
            return "keys_not_accepted"
        if len(keys) > policy.max_declared_regressions:
            return "too_many_keys"
        assert best is not None
        txn = RefactorTransaction(
            transaction_id=f"TXN-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            declared_regression_keys=keys,
            rationale=rationale,
            opened_at_loop=loop_id,
            deadline_loops=policy.refactor_deadline_loops,
            baseline_best_state_json=best.model_dump_json(),
            baseline_accepted_check_keys=list(best.accepted_check_keys),
        )
        best_dir = self.workspace_manager.best_files_dir(task_id)
        shutil.copytree(best_dir, self._snapshot_dir(task_id, txn.transaction_id))
        self.repo.save_refactor_transaction(txn)
        self.repo.append_event(
            "REFACTOR_TXN_OPENED",
            {"transaction_id": txn.transaction_id, "declared": keys,
             "rationale": rationale},
            task_id=task_id, loop_id=loop_id,
        )
        return txn

    def settle_if_due(
        self, task_id: str, loop_id: int, *, force: bool = False
    ) -> str | None:
        txn = self.repo.get_open_refactor_transaction(task_id)
        if txn is None:
            return None
        if not force and loop_id < txn.opened_at_loop + txn.deadline_loops:
            return None
        best = self.repo.get_best_state(task_id)
        accepted = set(best.accepted_check_keys) if best else set()
        baseline = set(txn.baseline_accepted_check_keys)
        r_repassed = set(txn.declared_regression_keys) <= accepted
        net_new = bool(accepted - baseline)
        snapshot = self._snapshot_dir(task_id, txn.transaction_id)
        if r_repassed and net_new:
            self.repo.update_refactor_transaction_status(
                txn.transaction_id, "closed_success"
            )
            self.repo.append_event(
                "REFACTOR_TXN_CLOSED_SUCCESS",
                {"transaction_id": txn.transaction_id},
                task_id=task_id, loop_id=loop_id,
            )
            shutil.rmtree(snapshot, ignore_errors=True)
            return "closed_success"
        best_dir = self.workspace_manager.best_files_dir(task_id)
        shutil.rmtree(best_dir, ignore_errors=True)
        shutil.copytree(snapshot, best_dir)
        shutil.rmtree(snapshot, ignore_errors=True)
        self.repo.save_best_state(
            BestState.model_validate_json(txn.baseline_best_state_json)
        )
        self.repo.update_refactor_transaction_status(
            txn.transaction_id, "rolled_back"
        )
        self.repo.append_event(
            "REFACTOR_TXN_ROLLED_BACK",
            {"transaction_id": txn.transaction_id},
            task_id=task_id, loop_id=loop_id,
        )
        return "rolled_back"
```

Wire the rest:
1. `models/worker.py`: add `"refactor_proposal"` to the `HandoffItemType` Literal (line 18-24).
2. `handoff_processor.py`: new branch before `discovered_issue` — `if item.item_type == "refactor_proposal":` call `self.refactor_transaction_manager.open(task_id, loop_id, item.related_check_keys, self._handoff_text(item))` when `item.summary.strip().lower() != "close"`, else `settle_if_due(task_id, loop_id, force=True)`; constructor gains `refactor_transaction_manager: RefactorTransactionManager | None = None` (skip branch when `None`).
3. `stagnation_detector.py` `update(...)`: add keyword `regression_exempt_item_ids: set[str] | None = None`; in the `for iid in attempted:` loop, `if regression_exempt_item_ids and iid in regression_exempt_item_ids: continue` before the increment branch.
4. `loop_orchestrator.py`: fetch `open_txn = self.repo.get_open_refactor_transaction(task_id)` when `policy.refactor_transactions_enabled`; pass `open_transaction=open_txn` to `CommitManager.apply`; pass `regression_exempt_item_ids={k.split(":", 1)[0] for k in open_txn.declared_regression_keys} if open_txn else None` to `StagnationDetector.update`; call `self.refactor_transaction_manager.settle_if_due(task_id, loop_id)` at end-of-loop (after stagnation update). Factory (`orchestrator_factory.py`) constructs the manager with the existing `WorkspaceManager` instance.

- [ ] **Step 4: Integration test (SQLite, rollback path)** — `tests/integration/test_refactor_rollback_sqlite.py`: build a real `SQLiteRepository` + `WorkspaceManager` in `tmp_path` (mirror `tests/integration/test_mission_run_single_worker.py` setup), seed best with one file + BestState, open a txn, mutate `best/files/m.py`, force settle with R unmet, assert file content restored byte-identical and `get_open_refactor_transaction` is `None`.

- [ ] **Step 5: Run** — unit + integration + `tests/unit/test_stagnation_detector.py -q` green; mypy; ruff.

- [ ] **Step 6: Commit** — `git commit -m "feat: RefactorTransactionManager with snapshot rollback and orchestrator wiring"`

---

## Phase 3b — Memory promote + recall

### Task 12: Candidate content upgrade + `auto_promote`

**Files:**
- Modify: `src/hungerloop/services/memory_manager.py`, `src/hungerloop/repository/protocol.py` + both repos (`list_memory_candidates(task_id)`, `list_promoted_memories(limit)` — check `memory_cmd.py` first: reuse existing list/promote plumbing if present instead of adding duplicates), `src/hungerloop/services/loop_orchestrator.py` (call site after DONE StopReport)
- Test: `tests/unit/test_memory_auto_promote.py`

**Interfaces:**
- Produces: `MemoryManager.auto_promote(task_id: str) -> list[str]` (promoted candidate ids; no-op unless all four predicates true per candidate); `propose_from_loop` content becomes `f"{check_key} [{item_title}] {check_description}"` clipped to 300 chars with any literal `task_id` occurrence replaced by `"<task>"` (keeps `reusable` true).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_memory_auto_promote.py
from __future__ import annotations

# Setup mirrors tests/unit/test_memory_manager.py (read it first): a repo
# with a DONE stop report whose final_best_state_id matches the candidate's
# source_best_state_id, and best.evidence_ids covering candidate evidence.


def test_content_is_descriptive_not_bookkeeping(memory_env):
    repo, manager, validation = memory_env(check_desc="nested group numbering")
    cands = manager.propose_from_loop("t1", 2, validation)
    assert "nested group numbering" in cands[0].content
    assert "t1" not in cands[0].content  # task-specific token stripped


def test_auto_promote_promotes_only_all_true(memory_env):
    repo, manager, validation = memory_env(check_desc="d")
    cands = manager.propose_from_loop("t1", 2, validation)
    promoted = manager.auto_promote("t1")
    assert promoted == [cands[0].candidate_id]
    assert len(repo.list_promoted_memories(limit=10)) == 1
    # second run is idempotent
    assert manager.auto_promote("t1") == []
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** — in `propose_from_loop` (memory_manager.py:137-155), replace `content=f"Verified acceptance check {check_key}"` with a helper `_describe(check_key)` that loads the ledger item (`self.repo.get_hunger_ledger(task_id)`), formats `f"{check_key} [{item.title}] {item.acceptance_checks[idx].description}"`, replaces `task_id` with `"<task>"`, clips to 300. Add:

```python
    def auto_promote(self, task_id: str) -> list[str]:
        """Promote every candidate whose four §19.2 predicates are all true."""
        policy = self.repo.get_hunger_policy(task_id)
        if not policy.memory_auto_promote_enabled:
            return []
        promoted: list[str] = []
        for candidate in self.repo.list_memory_candidates(task_id):
            if candidate.state != "proposed":
                continue
            best = self.repo.get_best_state(task_id)
            best_evidence = list(best.evidence_ids) if best else []
            if not (
                action_verified(candidate, best_evidence)
                and reusable(candidate)
                and non_volatile(candidate, self.repo)
                and traceable(candidate, best_evidence)
            ):
                continue
            self.repo.promote_memory_candidate(candidate.candidate_id)
            promoted.append(candidate.candidate_id)
        if promoted:
            self.repo.append_event(
                "MEMORY_AUTO_PROMOTED", {"candidate_ids": promoted}, task_id=task_id,
            )
        return promoted
```

`promote_memory_candidate` MUST reuse the exact promotion write that `cli/memory_cmd.py`'s manual `approve` performs (read it; if that logic lives in the CLI, extract it into a repo method both call — do not duplicate promotion semantics). Call site: `loop_orchestrator.py`, immediately after the StopReport is persisted with `stop_reason is StopReason.DONE` → `self.memory_manager.auto_promote(task_id)`.

- [ ] **Step 4: Run** — new tests + `tests/unit/test_memory_manager.py` + `tests/integration/test_memory_lifecycle_restart.py -q` green.

- [ ] **Step 5: Commit** — `git commit -m "feat: descriptive memory candidates + predicate-gated auto_promote"`

### Task 13: Cross-task recall into `ContextPack`

**Files:**
- Modify: `src/hungerloop/models/context.py` (`ContextPack`), `src/hungerloop/services/context_builder.py` (`build_for_agent`), `src/hungerloop/services/execution_worker.py` (render block)
- Test: `tests/unit/test_context_recall.py`

**Interfaces:**
- Consumes: `repo.list_promoted_memories(limit)` (Task 12) — returns promoted rows newest-first ACROSS tasks, each exposing `.content: str`.
- Produces: `ContextPack.recalled_memories: list[str] = Field(default_factory=list)` (total rendered budget 1200 chars, max 5 entries).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_context_recall.py
from __future__ import annotations

# Build ContextBuilder exactly as tests/unit/test_context_builder.py does
# (fake WorkspaceReader + InMemoryRepository). Seed 7 promoted memories on
# the repo (any task_id) with contents "M1".."M7".


def test_recall_top5_newest_first(ctx_env):
    builder, kwargs = ctx_env(promoted=[f"M{i}" for i in range(1, 8)])
    pack = builder.build_for_agent(**kwargs)
    assert pack.recalled_memories == ["M7", "M6", "M5", "M4", "M3"]


def test_recall_respects_policy_flag(ctx_env):
    builder, kwargs = ctx_env(promoted=["M1"], recall_enabled=False)
    assert builder.build_for_agent(**kwargs).recalled_memories == []


def test_recall_char_cap(ctx_env):
    builder, kwargs = ctx_env(promoted=["x" * 900, "y" * 900])
    pack = builder.build_for_agent(**kwargs)
    assert sum(len(m) for m in pack.recalled_memories) <= 1200
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement** — `ContextPack` gains `recalled_memories: list[str] = Field(default_factory=list)`. In `context_builder.py` `build_for_agent`, after the `passed_check_keys` computation (line ~198) insert:

```python
        recalled_memories: list[str] = []
        policy = self.repo.get_hunger_policy(task_id)
        if policy.memory_recall_enabled:
            remaining = 1200
            for row in self.repo.list_promoted_memories(limit=5):
                content = row.content[:remaining]
                if not content:
                    break
                recalled_memories.append(content)
                remaining -= len(content)
```

and pass `recalled_memories=recalled_memories` into the `ContextPack(...)` constructor call (line ~200). In `execution_worker.py`, find where `failure_patterns_to_avoid` is rendered into the system/user message (search the string `failure_patterns`) and add an equivalent block: `if context.recalled_memories: lines.append("Reusable insights from prior missions:"); lines.extend(f"- {m}" for m in context.recalled_memories)`.

- [ ] **Step 4: Run** — new tests + `tests/unit/test_context_builder*.py` + `tests/unit/test_execution_worker.py -q` green.

- [ ] **Step 5: Commit** — `git commit -m "feat: cross-task promoted-memory recall into ContextPack (capped)"`

---

### Task 14: Final gate + placeholder spec updates

**Files:**
- Modify: `specs/v0.7_placeholders/llm_planner.md`, `specs/v0.7_placeholders/concurrent_fan_out_and_join.md`, `specs/v0.7_placeholders/cross_task_memory_recall.md`

- [ ] **Step 1:** Append one line to each of the three placeholder files: `Partially implemented by specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md (see design for delivered scope).`
- [ ] **Step 2: Full verification**

```bash
python -m pytest tests/ -q
mypy --strict src/
ruff check src/ tests/
hungerloop --version
```

Expected: unit+integration green except the 1 pre-existing `test_loop_orchestrator` failure; mypy 0 errors; ruff clean.

- [ ] **Step 3:** Fix anything the gate surfaces (new failures only), re-run until clean.
- [ ] **Step 4: Commit** — `git commit -m "docs: link v0.7 placeholders to loop-objective evolution delivery"`

## Self-Review Notes (already applied)

- Spec coverage: Phase 1 → Tasks 1-5; Phase 3a → Tasks 6-7; Phase 2 → Tasks 8-11 (ADR + CLAUDE.md in Task 10); Phase 3b → Tasks 12-13; cross-cutting gate → Task 14. Settlement/rollback integration test on SQLite: Task 11 Step 4.
- Type consistency: `CheckProposal.dedup_key()`, `GateResult`, `compile_spec_coverage(...) -> list[str]`, `RefactorTransaction`, `CandidateEvaluation`, `select_commit_candidate`, `auto_promote -> list[str]`, `recalled_memories: list[str]` used consistently across tasks.
- Known verify-at-site points (each flagged inside its task): `process_handoffs` sync/async decision (Task 6), SQLiteRepository test constructor (Task 8), migration file naming (Task 8), `memory_cmd.py` promotion reuse (Task 12), ModelClient/SandboxRunner adapter method names (Task 5).
