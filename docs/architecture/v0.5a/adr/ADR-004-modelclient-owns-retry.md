# ADR-004: ModelClient owns retry; WorkerRuntime catches once

## Status
Accepted (2026-05-02)

## Context

`BudgetAllocation.max_model_retries / retry_base_delay_seconds / retry_max_delay_seconds` (PRD §4.2) describe LLM retry semantics. Per M6, the v0.5.2 spec described retry policy in §11.4 but never implemented it — neither `complete_json` (§11.2) nor `WorkerRuntime` (§7.3) had a retry loop.

The architectural question: where does the retry loop live?

Three candidates:

- (a) Inside `ModelClient.complete_json` — scoped to one HTTP call.
- (b) In `WorkerRuntime` — re-runs the entire worker on retryable errors.
- (c) In `ExecutionWorker` — wraps each `complete_json` call.

## Decision

**Retry lives inside `ModelClient.complete_json`.** WorkerRuntime catches `ModelCallError` exactly once and converts to `WorkerResult.error`.

```python
async def complete_json(
    self, *, task_id, agent_id, messages, max_tokens,
    max_retries=0,  # injected from BudgetAllocation
    retry_base_delay_seconds=1.0,
    retry_max_delay_seconds=20.0,
) -> ModelResponse:
    self.cost_guard.assert_within_budget(task_id)
    last_error: ModelCallError | None = None
    async with httpx.AsyncClient(timeout=...) as client:
        for attempt in range(max_retries + 1):
            try:
                return await self._call_once(...)
            except ModelRateLimitError as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._delay_for_rate_limit(exc.retry_after, attempt))
            except ModelCallError as exc:
                last_error = exc
                if not exc.retryable or attempt >= max_retries:
                    break
                await asyncio.sleep(self._exp_backoff(attempt))
    raise last_error
```

WorkerRuntime catches `ModelCallError` (any subclass) and produces a single `WorkerResult` with `error_type` set:

```python
except ModelAuthError as exc:
    return WorkerResult(..., requires_human=True, error_type="auth_error", retryable=False)
except ModelCallError as exc:
    return WorkerResult(..., error_type="model_call_error", retryable=exc.retryable)
```

## Alternatives Considered

### A. WorkerRuntime retries the whole worker
Catch `ModelCallError(retryable=True)` and re-invoke `worker.run(context, workspace_root)`.
- **Rejected** — re-runs all prior tool calls in the worker. Tool calls have side effects (file writes, subprocess spawns); replaying them is unsafe and wastes budget. Idempotency would have to be solved per-tool, which is a much larger problem than retrying one HTTP request.

### B. ExecutionWorker wraps each ModelClient call
Worker contains the loop.
- **Rejected** — every Worker subclass would re-implement the same loop. Easy to drift; impossible to enforce uniform retry semantics across LearningWorker / ResearchWorker (v0.5d) without copy-paste.

### C. No retry; let StagnationDetector handle it
A single 429 → loop fails → no-progress streak → eventually BLOCKED.
- **Rejected** — first user with a real OpenAI key will hit a 429 in their first 3 minutes and the entire task BLOCKs. Retry is table stakes for an LLM client.

### D. External retry library (`tenacity`, `backoff`)
- **Rejected for v0.5b** — adds a dependency for ~30 LOC of code. Reconsider if retry policy grows complex.

## Consequences

**Positive**
- Retry granularity matches the unit of work: a single API call. No re-execution of prior side effects.
- WorkerRuntime stays simple — one `try/except` block, no loop.
- `BudgetAllocation` retry params are read once per `complete_json` call; transparent to the worker.
- DummyModelClient ignores `max_retries` (no real failure modes), so v0.5a tests don't depend on retry semantics.

**Negative**
- ModelClient interface grows three retry parameters. Mitigation: bundle into a single `RetryPolicy` dataclass if it grows further.
- Each provider implementation must implement retry. Mitigation: extract `_AbstractRetryingModelClient` base class once a second provider lands (v0.5b+ Azure).
- A truly-stuck retry loop could hold an HTTP connection for `max_retries × retry_max_delay_seconds` (default 2 × 20 = 40s). Mitigation: Worker timeout (`asyncio.wait_for` in WorkerRuntime, ADR-002 sibling) bounds wall-clock.

## Trade-offs

Correctness of retry granularity > ModelClient interface simplicity. The three retry params on the function signature are a small price for not corrupting candidate workspace state via worker re-runs.

## Compliance

- `ModelCallError` carries `retryable: bool`; `ModelRateLimitError` is always `retryable=True`; `ModelAuthError` is always `retryable=False`.
- `WorkerRuntime` catches `ModelCallError` exactly once. No retry in WorkerRuntime.
- `complete_json` MUST honor `Retry-After` header on 429; if present and parseable, prefer it over exponential backoff.
- `complete_json` MUST call `cost_guard.assert_within_budget(task_id)` ONCE before the retry loop, and `cost_guard.record_llm_usage(task_id, usage)` exactly once per *successful* call (not per attempt — failed attempts cost 0).
- Test `test_openai_model_client_retry.py` covers: 429 → retry → success; 5xx → retry until exhaustion → raise; 401 → no retry.
