# ADR-003: ToolHarness is the policy layer; SandboxRunner is the execution layer

## Status
Accepted (2026-05-02)

## Context

v0.4.1 has `SandboxRunner` (services/sandbox_runner.py:33), which executes argv with timeout, output cap, and process-group cleanup. It produces `shell_output` evidence directly.

v0.5.2 §9 introduces `ToolHarness`, which workers call instead of arbitrary code. ToolHarness must enforce:

- Tool name registry (only known tools dispatch).
- `BudgetAllocation.allow_shell / allow_file_write / allow_network` policy gates (PRD §28.11).
- `path_safety.resolve_workspace_path` for every path argument.
- `cwd` forced to candidate workspace root.
- `tool_call` evidence (PRD §28.5 / M18).
- `BudgetGuard` interactions (ADR-002).

The question: should ToolHarness *be* SandboxRunner (one class), or call it (two layers)?

## Decision

Two layers, one direction:

```text
Worker  →  ToolHarness  →  SandboxRunner  (subprocess)
         (policy)        (execution)
```

- **ToolHarness** holds: tool registry, budget gates, path resolution, evidence emission, retry/no-retry policy, structured arg validation.
- **SandboxRunner** holds: subprocess spawn, asyncio timeout, process-group SIGKILL, output capture/decode/truncate, shell evidence (sandbox_run row).
- Workers MAY NOT import `SandboxRunner` directly; only `ToolHarness` does.
- ToolHarness's `RunShellTool.run(...)` delegates to `SandboxRunner.run_argv(...)` — no subprocess code in ToolHarness.

```python
class ToolHarness:
    def __init__(self, repo, sandbox_runner, tool_registry, budget_guard, path_safety):
        ...

    async def execute(self, *, task_id, loop_id, agent_id, tool_name,
                      args, budget, workspace_root) -> ToolResult:
        # 1. registry lookup
        tool = self._registry.get(tool_name)
        if tool is None:
            raise ToolNotPermitted(f"unknown tool: {tool_name}")

        # 2. policy gates
        if tool.side_effect_level == "shell" and not budget.allow_shell:
            raise ToolNotPermitted("shell disabled by budget")
        # ... allow_file_write, allow_network

        # 3. budget pre-check
        self.budget_guard.assert_can_spend(context, addl_tool_calls=1)

        # 4. path safety (for all path args)
        resolved_args = self._resolve_paths(args, workspace_root)

        # 5. dispatch (RunShellTool calls SandboxRunner internally)
        result = await tool.run(**resolved_args, cwd=workspace_root)

        # 6. budget record + tool_call evidence
        self.budget_guard.record(..., tool_calls=1)
        evidence_id = self.repo.save_tool_call_as_evidence(
            task_id=task_id, loop_id=loop_id, agent_id=agent_id,
            tool_name=tool_name,
            args_summary=summarize(resolved_args),
            result_summary=summarize(result),
            success=result.ok,
            elapsed_ms=result.elapsed_ms,
        )
        return ToolResult(evidence_ids=[evidence_id], ...)
```

## Alternatives Considered

### A. Merge ToolHarness into SandboxRunner
Single class handles both shell execution and policy.
- **Rejected** — mixes concerns. Policy (registry, allow_*, path_safety) must apply to non-shell tools too (`read_file`, `write_file`, `patch_file`); SandboxRunner's job is subprocess-only. Merging would either inflate SandboxRunner or scatter policy across tool classes.

### B. Workers call SandboxRunner directly with policy duplicated per worker
- **Rejected** — DRY violation, easy to forget a check, breaks I-7 (sandbox isolation invariant) the moment one worker ships without the policy.

### C. ToolHarness owns subprocess code; deprecate SandboxRunner
- **Rejected** — SandboxRunner is already battle-tested (process-group SIGKILL, output truncation, tested by `test_sandbox_runner.py`). Reuse > rewrite.

### D. Three layers (ToolHarness → ToolImpl → SandboxRunner)
Each tool is its own class; ToolHarness only does dispatch + policy.
- **Adopted partially** — registry holds Tool instances (ReadFileTool, WriteFileTool, RunShellTool), but they are thin (~30 LOC each). Not a separate ADR-worthy layer.

## Consequences

**Positive**
- Clear boundary: any change to "what's allowed" is a ToolHarness change; any change to "how shell runs" is a SandboxRunner change.
- Policy is unit-testable without spawning subprocesses (mock SandboxRunner).
- `RunShellTool` can be swapped for a different sandbox impl (Docker, firecracker) without touching ToolHarness or workers.
- Lint rule (`ruff` custom or `import-linter`) can ban `import subprocess` outside `services/sandbox_runner.py`, enforcing the boundary statically.

**Negative**
- One extra hop on every shell call. Negligible — subprocess spawn dominates.
- Two registries to keep in sync (TOOL_REGISTRY in ToolHarness; allowed_tools in AgentSpec). Mitigation: ToolHarness validates that `tool_name in spec.allowed_tools` at execute time and rejects with `ToolNotPermitted`.

## Trade-offs

Layering cost (one indirection) < testability + invariant enforcement. The 4 lines of code "saved" by merging would cost a measurable share of v0.5a tests' clarity.

## Compliance

- `subprocess` / `os.system` / `os.popen` MAY only appear in `services/sandbox_runner.py`. Add `ruff` rule or `import-linter` config to enforce.
- Every Tool class declares `side_effect_level: Literal["read_only", "file_write", "shell"]` and `requires_network: bool` as class attributes. ToolHarness uses these for `allow_*` gating.
- `tool_call` evidence MUST be written by ToolHarness, not by individual Tool classes — single source of truth.
- New tool registration in v0.5a is code-only (frozen at startup). Dynamic tool registration deferred to v0.6+.
