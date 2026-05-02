# ADR-006: Single RepositoryProtocol (fat interface)

## Status
Accepted (2026-05-02)

## Context

After v0.5.2 §28.5, §28.9, §28.14 add `save_tool_call_as_evidence`, `save_accepted_check`, and the new `task_id`/`loop_id` parameters on `append_event`, the `RepositoryProtocol` (currently `src/hungerloop/repository/protocol.py`, 67 lines) grows to ~30 methods spanning:

- HungerLedger / HungerPolicy / HungerClock / HungerSnapshot
- BestState / CandidateState / ValidationReport
- Evidence (sandbox_run, model_call, model_error, tool_call, validation_check, human_input)
- AgentSpec / WorkerResult / LoopPlan / LoopTrace
- StopReport / events / no_progress_streak / approvals
- MemoryCandidate / SkillCard

The architectural question: keep a single fat protocol, or split it into focused protocols (Interface Segregation Principle)?

Possible splits:

- `HungerRepository` (policy, clock, ledger, items, snapshots)
- `WorkspaceRepository` (best, candidates)
- `ValidationRepository` (validation reports, accepted checks, failures, regression)
- `EvidenceRepository` (all save_*_as_evidence methods, count, get)
- `WorkerRepository` (agent specs, worker results, loop plans)
- `TraceRepository` (loop traces, stop reports, events, usage snapshots)
- `MemoryRepository` (memory candidates, skill cards)

## Decision

**Keep a single `RepositoryProtocol`** for v0.5a. SQLiteRepository implements it as one class. Group methods in the protocol declaration with section comments matching the categories above for readability.

## Alternatives Considered

### A. Split into 7 focused protocols
Each service depends only on the protocols it needs.
- **Rejected for v0.5a** because:
  1. `LoopOrchestrator` orchestrates everything and would depend on all 7 protocols. Wiring cost (constructor takes 7 params instead of 1) outweighs decoupling benefit when there's only one implementation.
  2. Test mocks multiply: every test that uses `LoopOrchestrator` must instantiate 7 mock objects.
  3. Cross-protocol invariants (e.g., `save_best_state` should also write `accepted_checks`) become harder to express atomically — you'd need to re-introduce a transaction context spanning protocols.
  4. ADR-001's atomic transaction for "commit" spans HungerRepository + WorkspaceRepository + ValidationRepository + EvidenceRepository — splitting forces a transaction boundary back into the orchestrator that the single protocol could own.

### B. Service-specific narrow protocols
Each service defines its own `Protocol` reflecting only the methods it calls (e.g., `class CommitManagerRepo(Protocol)` with 5 methods). SQLiteRepository structurally satisfies all of them.
- **Considered** — better testability (mocks only need 5 methods). Rejected because (a) Pydantic Protocol composition with structural typing is well-supported by mypy but (b) it scatters the source of truth — the actual schema lives in `RepositoryProtocol` and 13 narrow protocols would drift from it without enforcement.
- **Revisit at v0.6+** when the protocol size becomes a real type-check pain point.

### C. Two protocols: `ReadRepository` + `WriteRepository`
CQRS-style split.
- **Rejected** — most services do both. Doubles the wiring with no clear benefit.

### D. Active Record on entity classes
Move `save()` onto `BestState`, `HungerItem`, etc.
- **Rejected** — violates the project's "frozen models, behavior in services" convention (see CLAUDE.md). Models stay data-only.

## Consequences

**Positive**
- Service constructors take a single `repo: RepositoryProtocol` parameter — current shape is preserved.
- One `InMemoryRepository` implementation suffices for tests; no mock juggling.
- Cross-cutting transactions (commit promotes BestState + writes accepted_checks + marks candidate committed) live as one SQLiteRepository method, atomic.
- Refactoring within v0.5a is local: adding a new method touches `protocol.py`, `in_memory_repo.py`, and `sqlite_repo.py`.
- Section comments in `protocol.py` provide "soft" segregation that humans can follow without changing the type structure.

**Negative**
- Protocol grows unbounded. ~30 methods now; could reach 50+ by v0.6. Mitigation: re-evaluate split at every major version. Pre-commit lint can warn when protocol exceeds a configured method count threshold.
- Any service that depends on `RepositoryProtocol` is "coupled" to all of it, including methods it never calls. Mitigation: this is structural, not behavioral coupling — Python doesn't enforce method-level dependency tracking, so the practical impact is zero.
- A future reader scanning `protocol.py` sees a wall of methods. Mitigation: section comments + a cross-reference table in this ADR (below).

## Trade-offs

Wiring simplicity + atomic transactions > Interface Segregation purity. The textbook "fat interface" critique assumes multiple implementations and mocks; v0.5a has one implementation and InMemoryRepository for tests. ISP cost is low here.

## Compliance

- `protocol.py` MUST organize methods in this section order with `# === Section ===` comments:
  1. Hunger (policy, clock, ledger, items, snapshots)
  2. Workspace state (best, candidates, last phase)
  3. Validation (reports, accepted_checks, failures, items_for_check_keys)
  4. Evidence (save_*_as_evidence, count, artifacts)
  5. Worker / Planning (agent_spec, worker_result, loop_plan)
  6. Trace / Stop (loop_traces, stop_reports, events, usage_snapshot)
  7. Memory / Skill (memory_candidate, skill_card, count_committed_references)
  8. Approvals & misc (is_approval_granted, no_progress_streak, next_loop_id, transaction)
- `mypy --strict` must pass against the protocol with `disallow_any_decorated = true`.
- When method count crosses 40, open an issue tagged `architecture-debt:repository-protocol-split` referencing this ADR; revisit before merging.
- The existence of `transaction()` context manager method is mandatory (per ADR-001) — cross-cutting writes use it.

## Section Index (for navigation)

| Section | Method count (target) | Key methods |
|---|---|---|
| Hunger | 9 | get_hunger_policy, get_hunger_clock, save_hunger_clock, get_hunger_ledger, get_hunger_item(s), save_hunger_item, save_hunger_snapshot, get_last_phase |
| Workspace state | 4 | get_best_state, save_best_state, save_candidate, mark_candidate_{committed,rejected} |
| Validation | 4 | save_validation_report, add_failure_from_validation, get_items_for_check_keys, save_accepted_check |
| Evidence | 6 | save_shell_output_as_evidence, save_model_call_as_evidence, save_model_error_as_evidence, save_tool_call_as_evidence, count_evidence_by_type, get_artifacts_by_ids |
| Worker / Planning | 4 | get_agent_spec, save_agent_spec, save_worker_result, save_loop_plan |
| Trace / Stop | 4 | save_loop_trace, save_stop_report, append_event, get_usage_snapshot |
| Memory / Skill | 5 | save_memory_candidate, list_memory_candidates, count_committed_references, save_skill_card, list_skill_cards |
| Approvals & misc | 5 | is_approval_granted, reset_no_progress_streak, increment_no_progress_streak, next_loop_id, get_last_stop_reason, transaction |
| **Total** | **~41** | (revisit split at 50) |
