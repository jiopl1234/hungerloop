# ADR-002: BudgetGuard state is process-local in-memory, not persisted

## Status
Accepted (2026-05-02)

## Context

PRD §28.4 (M12) requires `BudgetGuard` to be stateful: per-loop / per-worker token / tool_call counters that prevent a worker from exceeding its phase budget mid-loop. The earlier stateless design (v0.5.2 §4.4) was effectively a no-op — every `assert_worker_budget(estimated=0)` call passed.

Open question: where does the running counter live?

- (a) in-memory dict inside `BudgetGuard` instance
- (b) persisted to SQLite per call (durable, audit trail)
- (c) folded into `CostGuard.HungerClock` (couples task ceiling and loop scope)

Constraints:

- `BudgetGuard.assert_can_spend(...)` and `record(...)` are called multiple times per worker invocation (every LLM call, every tool call). Hot path.
- Crash recovery: if the orchestrator dies mid-loop, the loop is rejected on resume (no committed validation report → candidate workspace discarded). The full `BudgetAllocation` is "spent" from the accounting POV regardless of partial usage, because `clock.loop_count++` already happened at loop start (PRD §4.2).
- v0.5a is single-process. No cross-process budget sharing required.

## Decision

`BudgetGuard` keeps an **in-memory dict** keyed by `(task_id, loop_id, agent_id)`:

```python
class BudgetGuard:
    def __init__(self) -> None:
        self._usage: dict[tuple[str, int, str], BudgetUsage] = {}

    def reset(self, task_id, loop_id, agent_id) -> None: ...
    def record(self, task_id, loop_id, agent_id, *, tokens=0,
               tool_calls=0, llm_calls=0, elapsed_seconds=0.0) -> None: ...
    def assert_can_spend(self, context: ContextPack, *,
                         addl_tokens=0, addl_tool_calls=0,
                         addl_llm_calls=0) -> None: ...
```

- `reset` is called at `WorkerRuntime.run` entry, evicting any stale entry for that triple.
- `record` is called by `ModelClient.complete_json` (post-call, with usage tokens) and `ToolHarness.execute` (post-call, with `tool_calls=1`).
- `assert_can_spend` is called pre-call by the same two sites with `addl_*` projections.

State is **never** written to SQLite or read back across process restarts.

## Alternatives Considered

### A. Persist to SQLite per call
Add `budget_usage` table; write on every record/assert.
- **Rejected** — adds 2× DB round-trips to every LLM/tool call (hot path). No recovery benefit: a crash mid-loop discards the candidate, so partial usage is moot. Audit trail is already provided by `LoopTrace.tokens_consumed_this_loop` and the `evidence` table — those are the durable record.

### B. Fold into CostGuard
Reuse `HungerClockState.consumed_tokens` and gate phase budget against it.
- **Rejected** — couples two scopes that decay differently. CostGuard tracks task-cumulative spend (never resets); BudgetGuard tracks per-(loop, worker) spend (resets every loop). Conflating them invites bugs where one scope's reset wipes the other.

### C. Stateless validator (v0.5.2 §4.4 original)
Caller passes "cumulative usage" each call.
- **Rejected** (already by M12) — pushes state management onto every caller, which means each caller re-implements it inconsistently.

### D. Async-context-manager scope
`async with budget_guard.scope(context): ...` with state held only inside the scope.
- **Considered** but rejected for v0.5a simplicity. Add later if WorkerRuntime grows nested scopes.

## Consequences

**Positive**
- Hot path is dict lookup + integer addition. ~no overhead.
- Implementation < 80 LOC; testable without SQLite.
- `BudgetGuard` instance can be unit-tested with synthetic ContextPacks.

**Negative**
- State lost on crash. Acceptable per the rejection of (A).
- Memory grows as `(task_id, loop_id, agent_id)` triples accumulate. Mitigation: explicit `reset` at WorkerRuntime entry purges any leaked entries; we also add a periodic `evict_completed_loops()` call from Orchestrator after `save_loop_trace`.
- BudgetGuard is process-local: a hypothetical multi-process orchestrator would need a different design. v0.5a is single-process; revisit at v0.6+ if needed.

## Trade-offs

Simplicity + hot-path performance > durability of an ephemeral counter. The durable record is `LoopTrace.tokens_consumed_this_loop` + `evidence` rows, which capture the same information at loop-end granularity — sufficient for post-mortem analysis.

## Compliance

- `WorkerRuntime.run` MUST call `budget_guard.reset(task_id, loop_id, agent_id)` before invoking the worker.
- `ModelClient.complete_json` MUST call `assert_can_spend(addl_llm_calls=1, addl_tokens=estimated)` pre-call and `record(tokens=actual, llm_calls=1)` post-call.
- `ToolHarness.execute` MUST call `assert_can_spend(addl_tool_calls=1)` pre-call and `record(tool_calls=1)` post-call.
- `LoopOrchestrator` MUST call `budget_guard.evict_completed_loops(task_id, keep=2)` after `save_loop_trace` to bound memory growth.
- Tests in `test_budget_guard.py` must cover: per-key isolation, reset clears, exceeding raises `WorkerBudgetExceeded`, eviction.
