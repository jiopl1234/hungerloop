# HungerLoop v0.5.2 PRD — 可运行 Orchestrator、模型 API、WorkerRuntime 与持久化闭环

**版本**：v0.5.2 corrected PRD  
**日期**：2026-05-02  
**基线**：v0.5.1 PRD + v0.4.1 当前代码实现 + Claude review N1–N13 / A–C 反馈  
**目标**：在不扩大 MVP 范围的前提下，补齐 v0.5a 开工前会导致实现分叉、运行错误或 schema 漏洞的问题。

---

## 0. 本版结论

v0.4.1 已经完成了核心 harness：

- check-level commit
- workspace isolation
- targeted validation
- sandbox runner
- cost guard
- stagnation detector
- BLOCKED 与 DONE 区分
- RuleBasedCompiler
- InMemoryRepository
- 基础 CLI 检查命令

v0.5.2 的目标不是推翻这些机制，而是把系统推进到“可以真正运行一个任务”的最小闭环：

```text
hungerloop new
  ↓
RuleBasedCompiler 生成 HungerLedger
  ↓
SQLiteRepository 持久化任务状态
  ↓
hungerloop run
  ↓
LoopOrchestrator.step
  ↓
RuleBasedPlanner 选中 active hunger item
  ↓
WorkerRuntime 调用 ExecutionWorker
  ↓
ExecutionWorker 使用 ModelClient / ToolHarness
  ↓
写 candidate workspace 与 evidence
  ↓
ValidationGate 执行 targeted checks
  ↓
CommitManager promote / reject
  ↓
HungerUpdate + StagnationDetector + LoopTrace
  ↓
StopReport
```

本版最重要的修正：

```text
1. StopReason 扩展正式列为 schema 变更。
2. RepositoryProtocol 新增方法集中列出，并同步 SQLite schema。
3. ContextPack.budget 从 dict 改为 BudgetAllocation。
4. 增加 RuleBasedPlanner 规格，避免实现者各自发挥。
5. 空 plan 不再立刻 BLOCKED 整个任务。
6. CLI --resume 前置检查职责明确。
7. EvidenceType 枚举固定，避免字符串散落。
8. SkillCard 触发条件固定。
9. Azure OpenAI v0.5a loader 直接拒绝，避免运行时才崩。
10. PricingTable 有硬编码规格与未知模型处理。
11. WorkerRuntime 规格补齐。
12. ToolHarness 规格补齐。
13. AgentSpec v0.5a 启动时硬编码注册 execution_worker_v1。
```

---

## 1. 当前实现基线

### 1.1 已完成

v0.4.1 当前代码已经包括：

```text
HungerEngine
HungerPolicy
HungerClockState
HungerLedger
WorkspaceManager
SandboxRunner
AcceptanceCheckRunner
ValidationGate
CommitManager
CostGuard
StagnationDetector
HungerUpdateService
RequirementCompiler
ContextBuilder
Integrator
InMemoryRepository
RepositoryProtocol
CLI: workspace / checks
```

### 1.2 明确缺失

当前版本还没有：

```text
LoopOrchestrator
WorkerRuntime
真实 ExecutionWorker
ModelClient
OpenAIModelClient
ModelConfig loader
PricingTable
BudgetGuard / phase budget tracking
hungerloop new
hungerloop run
hungerloop status
SQLiteRepository
MemoryManager
SkillCard
```

### 1.3 v0.5.2 的工程原则

```text
1. 先做可恢复、可测试、可停止的单 worker loop。
2. 不做 3×3 Worker。
3. 不做 LLMPlanner。
4. 不做 LLM-as-judge。
5. 不做自动 Memory Promotion。
6. 不做 FastAPI。
7. 不把 API key 明文写入 YAML。
8. 不让 Worker 直接改 BestState 或 CandidateState。
9. 不让空 plan 直接 BLOCKED 整个 task。
10. 所有新增 repository 方法必须进入 Protocol 与 SQLite schema。
```

---

## 2. v0.5.2 交付范围

### 2.1 v0.5a P0：真正能跑一轮的最小系统

```text
1. StopReason schema 扩展。
2. RepositoryProtocol 收紧，去除核心服务里的 repo: Any。
3. SQLiteRepository。
4. RuleBasedPlanner。
5. AgentSpecRegistry，硬编码 execution_worker_v1。
6. BudgetAllocation 升级，加入 wall-clock 与 worker budget。
7. ContextPack.budget 改为 BudgetAllocation。
8. WorkerRuntime。
9. DummyModelClient。
10. Minimal ExecutionWorker。
11. ToolHarness。
12. LoopOrchestrator.step / run。
13. CLI: new / run / status / resume preflight。
14. EvidenceType 枚举。
15. LoopTrace / StopReport 字段扩展。
16. gap_score 浮点收敛修正。
17. examples/demo_task.yaml。
```

### 2.2 v0.5b P0：真实模型与最小执行能力

```text
1. OpenAIModelClient。
2. ModelConfig loader。
3. api_key_env 读取。
4. PricingTable。
5. LLM retry / rate limit / error evidence。
6. ExecutionWorker 使用 LLM 生成 patch 或指令。
7. ToolHarness 支持 read_file / write_file / patch_file / run_shell。
8. CLI --model-config。
```

### 2.3 v0.5c P1：MemoryCandidate 与 SkillCard

```text
1. MemoryManager。
2. MemoryCandidate 生成。
3. SkillCard 生成。
4. memory list。
5. skill list。
6. deterministic demo memory output。
```

### 2.4 后置内容

```text
1. LearningWorker。
2. ResearchWorker。
3. LLMPlanner。
4. 多 worker 并发。
5. LLM-as-judge。
6. Azure OpenAI runtime。
7. FastAPI。
8. 自动 Memory Promotion。
9. 浏览器或桌面自动化。
```

---

## 3. Schema 变更

### 3.1 StopReason 扩展

当前 v0.4.1 的 `StopReason` 需要扩展。v0.5.2 明确要求在 `models/enums.py::StopReason` 中增加：

```python
class StopReason(str, Enum):
    DONE = "done"
    HUNGER_EXPIRED = "hunger_expired"
    BLOCKED = "blocked"
    HUMAN_REQUIRED = "human_required"  # NEW in v0.5.2
    HUMAN_PAUSED = "human_paused"
    SAFETY_STOP = "safety_stop"
    ERROR = "error"                    # NEW in v0.5.2
```

`HUMAN_REQUIRED` 用于身份认证、权限、审批或用户输入缺失。  
`ERROR` 用于不可恢复的系统错误，例如 repository I/O 错误、schema 损坏、未捕获异常。

### 3.2 WorkerResult 增加 requires_human

为了让 Worker → Orchestrator 的 HUMAN_REQUIRED 传递路径明确，`WorkerResult` 必须增加字段：

```python
class WorkerResult(BaseModel):
    agent_id: str
    task_id: str
    loop_id: int
    summary: str = ""
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)

    error: str | None = None
    error_type: str | None = None
    requires_human: bool = False
    retryable: bool = False
```

Orchestrator 规则：

```text
if any(worker_result.requires_human):
    stop_reason = HUMAN_REQUIRED
elif any(non_retryable worker_result.error):
    stop_reason = ERROR 或继续进入 validation，取决于是否仍有 evidence/artifact
```

兼容策略：如果旧 Worker 只返回字符串错误，则 Orchestrator 可临时按以下规则升级：

```python
if result.error and result.error.startswith(("auth_", "permission_", "approval_")):
    result.requires_human = True
```

但 v0.5.2 正式接口应使用 `requires_human`。

### 3.3 EvidenceType 枚举

新增 `EvidenceType`，不再让 evidence type 字符串散落在代码中。

```python
class EvidenceType(str, Enum):
    SANDBOX_RUN = "sandbox_run"
    MODEL_CALL = "model_call"
    MODEL_ERROR = "model_error"
    VALIDATION_CHECK = "validation_check"
    TOOL_CALL = "tool_call"
    HUMAN_INPUT = "human_input"
```

所有 evidence 写入方法都必须使用该枚举，或者使用等价的 `Literal` 类型。

```python
EvidenceTypeLiteral = Literal[
    "sandbox_run",
    "model_call",
    "model_error",
    "validation_check",
    "tool_call",
    "human_input",
]
```

---

## 4. Budget 模型与 ContextPack 升级

### 4.1 Budget hierarchy

v0.5.2 明确三层预算关系：

```text
Task Ceiling
  ↓
Loop Budget
  ↓
Worker / Phase Allocation
```

含义：

```text
Task Ceiling:
  由 HungerPolicy.max_total_cost_usd / max_total_tokens 控制。
  CostGuard 负责检查。

Loop Budget:
  每轮由 BudgetAllocator 根据 HungerSnapshot 生成。
  控制本轮最多多少 tokens、tool calls、wall-clock。

Worker / Phase Allocation:
  对单个 WorkerRuntime.run 的约束。
  控制单 worker 的 max_tokens、max_tool_calls、max_wall_clock_seconds。
```

### 4.2 BudgetAllocation 模型

```python
class BudgetAllocation(BaseModel):
    phase: LoopPhase

    max_tokens: int = 4000
    max_tool_calls: int = 8
    max_wall_clock_seconds: int = 300

    max_model_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 20.0

    allow_shell: bool = True
    allow_file_write: bool = True
    allow_network: bool = False

    max_new_branches: int = 0
    require_validation_first: bool = False
```

### 4.3 ContextPack.budget 类型变更

当前 v0.4.1 的 `ContextPack.budget` 是 `dict[str, object]`。v0.5.2 必须改成：

```python
class ContextPack(BaseModel):
    task_id: str
    loop_id: int
    agent_id: str
    mission: str
    phase: str

    target_hunger_item_ids: list[str]
    acceptance_criteria: list[str] = Field(default_factory=list)

    best_state_summary: str | None = None
    best_workspace_ref: str = "best"
    candidate_workspace_ref: str

    relevant_claim_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    failure_patterns_to_avoid: list[str] = Field(default_factory=list)

    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)

    budget: BudgetAllocation
    required_output_schema: str = ""
```

这样 `context.budget.max_tokens`、`context.budget.max_wall_clock_seconds` 这类属性访问才合法。

### 4.4 BudgetGuard

新增 `BudgetGuard`，与 `CostGuard` 区分：

```text
CostGuard: 检查 task-level 总成本。
BudgetGuard: 检查本轮 / worker 的局部预算。
```

```python
class WorkerBudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def assert_worker_budget(
        self,
        context: ContextPack,
        *,
        estimated_tokens: int = 0,
        estimated_tool_calls: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        if estimated_tokens > context.budget.max_tokens:
            raise WorkerBudgetExceeded("worker token budget exceeded")
        if estimated_tool_calls > context.budget.max_tool_calls:
            raise WorkerBudgetExceeded("worker tool call budget exceeded")
        if elapsed_seconds > context.budget.max_wall_clock_seconds:
            raise WorkerBudgetExceeded("worker wall-clock budget exceeded")
```

---

## 5. RuleBasedPlanner

### 5.1 为什么必须有 Planner 规格

v0.5.2 明确排除 LLMPlanner，但 Orchestrator 会调用：

```python
plan = planner.plan(task_id, loop_id, snapshot, budget)
```

因此 v0.5a 必须提供确定性的 `RuleBasedPlanner`。

### 5.2 选择规则

RuleBasedPlanner 的 MVP 规则：

```text
1. 从 HungerLedger.active_items() 中取出可执行项。
2. 按 priority × gap_score 降序排序。
3. 取前 N 个，N = min(1, budget.max_subagents)。v0.5a 固定只取 1 个。
4. 将该 item 路由到 execution_worker_v1。
5. mission 使用 item.title、acceptance_checks 和当前 phase 生成。
6. 若没有 active item，不直接 BLOCKED；返回空 plan，由 Orchestrator 走 no-progress 逻辑。
```

### 5.3 数据模型

```python
class Assignment(BaseModel):
    agent_id: str
    mission: str
    target_hunger_item_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)


class LoopPlan(BaseModel):
    task_id: str
    loop_id: int
    selected_hunger_item_ids: list[str] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    phase: LoopPhase
    rationale: str = ""
```

### 5.4 RuleBasedPlanner 伪代码

```python
class RuleBasedPlanner:
    def __init__(self, repo: RepositoryProtocol):
        self.repo = repo

    def plan(
        self,
        task_id: str,
        loop_id: int,
        snapshot: HungerSnapshot,
        budget: BudgetAllocation,
    ) -> LoopPlan:
        ledger = self.repo.get_hunger_ledger(task_id)
        items = sorted(
            ledger.active_items(),
            key=lambda item: item.priority * item.gap_score,
            reverse=True,
        )

        if not items:
            return LoopPlan(
                task_id=task_id,
                loop_id=loop_id,
                selected_hunger_item_ids=[],
                assignments=[],
                phase=budget.phase,
                rationale="No active hunger items available for planning.",
            )

        item = items[0]
        return LoopPlan(
            task_id=task_id,
            loop_id=loop_id,
            selected_hunger_item_ids=[item.id],
            assignments=[
                Assignment(
                    agent_id="execution_worker_v1",
                    mission=self._mission_for(item, snapshot.phase),
                    target_hunger_item_ids=[item.id],
                    allowed_tools=["read_file", "write_file", "patch_file", "run_shell"],
                )
            ],
            phase=budget.phase,
            rationale=f"Selected highest priority active item: {item.id}",
        )
```

### 5.5 空 plan 处理

空 plan 不得直接使整个 task 进入 BLOCKED。

正确流程：

```text
1. reject candidate workspace。
2. 保存 loop trace，committed=False，delta_summary="empty plan"。
3. StagnationDetector 增加 global no-progress streak。
4. 只有达到全局阈值时，StopReason.BLOCKED。
5. 如果 ledger.is_done()，HungerEngine 下一轮返回 DONE。
```

Orchestrator 不应执行：

```python
if not plan.assignments:
    return StopReport(BLOCKED)  # 禁止
```

应执行：

```python
if not plan.assignments:
    workspace_manager.reject_candidate(task_id, loop_id)
    global_blocked = repo.increment_no_progress_streak(task_id) >= max_global_no_progress
    if global_blocked:
        return build_stop_report(task_id, StopReason.BLOCKED)
    return LoopTrace(... next_action="continue", delta_summary="empty plan")
```

---

## 6. AgentSpecRegistry

### 6.1 v0.5a 注册规则

v0.5a 不实现动态 agent registry。启动时硬编码注册一个 AgentSpec：

```python
EXECUTION_WORKER_V1 = AgentSpec(
    agent_id="execution_worker_v1",
    name="ExecutionWorkerV1",
    kind="execution",
    output_schema_name="ExecutionWorkerResult",
    allowed_tools=["read_file", "write_file", "patch_file", "run_shell"],
)
```

该 spec 必须写入 repository，或由 `AgentSpecRegistry.get_agent_spec()` 返回。

### 6.2 Repository 调用

Orchestrator 中：

```python
spec = repo.get_agent_spec(assignment.agent_id)
```

若 spec 不存在：

```text
1. reject candidate。
2. save model/tool error as evidence。
3. return StopReport(ERROR)。
```

---

## 7. WorkerRuntime

### 7.1 角色定位

WorkerRuntime 是 Worker 的厚壳，不只是简单 dispatcher。

它负责：

```text
1. 根据 spec.agent_id 路由到具体 worker。
2. 调用前检查 CostGuard。
3. 调用前检查 BudgetGuard。
4. 用 asyncio.wait_for 包裹 worker.run。
5. 捕获 ModelCallError。
6. 捕获 WorkerBudgetExceeded。
7. 捕获 SafetyStopError。
8. 把可恢复错误转成 WorkerResult.error。
9. 把需要人类处理的错误标记为 requires_human=True。
```

### 7.2 接口

```python
class WorkerRuntime:
    def __init__(
        self,
        workers: dict[str, Worker],
        cost_guard: CostGuard,
        budget_guard: BudgetGuard,
        repo: RepositoryProtocol,
    ):
        self.workers = workers
        self.cost_guard = cost_guard
        self.budget_guard = budget_guard
        self.repo = repo

    async def run(
        self,
        spec: AgentSpec,
        context: ContextPack,
        workspace_root: Path,
    ) -> WorkerResult:
        ...
```

### 7.3 伪代码

```python
async def run(self, spec, context, workspace_root):
    worker = self.workers.get(spec.agent_id)
    if worker is None:
        return WorkerResult(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error="worker_not_registered",
            error_type="configuration",
            requires_human=False,
            retryable=False,
        )

    try:
        self.cost_guard.assert_within_budget(context.task_id)
        self.budget_guard.assert_worker_budget(context)

        return await asyncio.wait_for(
            worker.run(context=context, workspace_root=workspace_root),
            timeout=context.budget.max_wall_clock_seconds,
        )

    except SafetyStopError as exc:
        raise exc

    except WorkerBudgetExceeded as exc:
        return WorkerResult(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error=str(exc),
            error_type="worker_budget_exceeded",
            retryable=False,
        )

    except ModelAuthError as exc:
        evidence_id = self.repo.save_model_error_as_evidence(
            task_id=context.task_id,
            loop_id=context.loop_id,
            agent_id=spec.agent_id,
            provider="unknown",
            model="unknown",
            error_type="auth_error",
            error_message=str(exc),
            retryable=False,
        )
        return WorkerResult(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error=f"auth_error:{exc}",
            error_type="auth_error",
            requires_human=True,
            retryable=False,
            evidence_ids=[evidence_id],
        )

    except ModelCallError as exc:
        evidence_id = self.repo.save_model_error_as_evidence(...)
        return WorkerResult(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error=f"model_call_error:{exc}",
            error_type="model_call_error",
            retryable=exc.retryable,
            evidence_ids=[evidence_id],
        )

    except asyncio.TimeoutError:
        return WorkerResult(
            agent_id=spec.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            error="worker_timeout",
            error_type="timeout",
            retryable=True,
        )
```

---

## 8. Worker 规范

### 8.1 Worker 不直接写 CandidateState

Worker 不接收，也不修改 `CandidateState`。

Worker 的职责是：

```text
1. 读取 ContextPack。
2. 在 candidate workspace 内写文件。
3. 通过 ToolHarness 产生 evidence。
4. 返回 WorkerResult。
5. Integrator 用 WorkerResult 生成 CandidateState。
```

禁止：

```text
Worker 直接保存 BestState。
Worker 直接保存 CandidateState。
Worker 修改 best workspace。
Worker 绕过 ToolHarness 写任意路径。
```

### 8.2 ExecutionWorker v0.5a

v0.5a 的 ExecutionWorker 可以先使用 DummyModelClient，目标是打通 loop。

```python
class ExecutionWorker:
    def __init__(
        self,
        model_client: ModelClient,
        tool_harness: ToolHarness,
        repo: RepositoryProtocol,
    ):
        self.model_client = model_client
        self.tool_harness = tool_harness
        self.repo = repo

    async def run(self, context: ContextPack, workspace_root: Path) -> WorkerResult:
        # 1. Ask model for structured actions
        response = await self.model_client.complete_json(
            task_id=context.task_id,
            agent_id=context.agent_id,
            messages=self._messages(context),
            max_tokens=context.budget.max_tokens,
        )

        # 2. Execute tool actions
        evidence_ids = []
        artifact_ids = []
        for action in response.actions:
            result = await self.tool_harness.execute(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=context.agent_id,
                tool_name=action.tool_name,
                args=action.args,
                budget=context.budget,
                workspace_root=workspace_root,
            )
            evidence_ids.extend(result.evidence_ids)
            artifact_ids.extend(result.artifact_ids)

        return WorkerResult(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=response.summary,
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
        )
```

### 8.3 ExecutionWorker shell 与 path 规则

所有 shell 调用必须走 ToolHarness → SandboxRunner。

```text
cwd 强制 candidate workspace。
path 必须 resolve_workspace_path(candidate_workspace, path)。
best workspace 只读，不允许 worker 写入。
shell 不接受字符串 cmd，只接受 argv。
```

---

## 9. ToolHarness

### 9.1 ToolHarness 与 SandboxRunner 的关系

```text
ToolHarness 是工具入口与权限层。
SandboxRunner 是执行 shell 的底层 runner。

ToolHarness.run_shell → SandboxRunner.run_argv
```

ToolHarness 必须负责：

```text
1. 工具名注册表。
2. side-effect policy。
3. path safety。
4. cwd 强制 candidate workspace。
5. tool_call evidence。
6. max_tool_calls 计数。
7. shell 统一走 SandboxRunner。
```

### 9.2 MVP 工具清单

```python
TOOL_REGISTRY = {
    "read_file": ReadFileTool(),
    "write_file": WriteFileTool(),
    "patch_file": PatchFileTool(),
    "run_shell": RunShellTool(),
}
```

### 9.3 工具接口

```python
class ToolHarness:
    def __init__(
        self,
        repo: RepositoryProtocol,
        sandbox_runner: SandboxRunner,
        tool_registry: dict[str, Tool],
        budget_guard: BudgetGuard,
    ):
        ...

    async def execute(
        self,
        task_id: str,
        loop_id: int,
        agent_id: str,
        tool_name: str,
        args: dict[str, object],
        budget: BudgetAllocation,
        workspace_root: Path,
    ) -> ToolResult:
        ...
```

### 9.4 path safety

所有 path 参数必须经过：

```python
safe_path = resolve_workspace_path(workspace_root, user_path)
```

### 9.5 run_shell

```python
class RunShellTool:
    name = "run_shell"
    side_effect_level = "test_or_build"

    async def run(self, argv: list[str], cwd: Path, timeout: int) -> SandboxRunResult:
        return await sandbox_runner.run_argv(...)
```

禁止：

```json
{"cmd": "pytest tests/"}
```

允许：

```json
{"argv": ["pytest", "tests/"], "timeout": 60}
```

---

## 10. ModelConfig 与 API Key

### 10.1 禁止明文 key

配置文件不允许写：

```yaml
api_key: sk-...
```

必须写：

```yaml
provider: openai
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY
base_url: https://api.openai.com/v1
max_tokens: 4096
```

### 10.2 ModelConfig

```python
class ModelProvider(str, Enum):
    DUMMY = "dummy"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"


class ModelConfig(BaseModel):
    provider: ModelProvider = ModelProvider.DUMMY
    model_name: str = "dummy"
    api_key_env: str | None = None
    base_url: str | None = None
    timeout_seconds: int = 60
    max_tokens: int = 4096
    temperature: float = 0.1

    # Azure fields, v0.5a loader rejects runtime
    azure_endpoint: str | None = None
    azure_deployment: str | None = None
    azure_api_version: str | None = None
```

### 10.3 Loader 规则

```python
class ModelConfigLoader:
    def load(self, path: Path) -> ModelConfig:
        raw = yaml.safe_load(path.read_text())
        if "api_key" in raw:
            raise ValueError("Do not put literal API keys in config. Use api_key_env.")

        config = ModelConfig(**raw)

        if config.provider == ModelProvider.AZURE_OPENAI:
            raise NotImplementedError(
                "Azure runtime ships in v0.5b+; use openai or dummy in v0.5a"
            )

        if config.provider == ModelProvider.OPENAI:
            if not config.api_key_env:
                raise ValueError("openai provider requires api_key_env")
            if not os.getenv(config.api_key_env):
                raise ModelAuthError(
                    f"Environment variable {config.api_key_env} is not set"
                )

        return config
```

---

## 11. ModelClient

### 11.1 Interface

```python
class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ModelResponse(BaseModel):
    content: str
    json_data: dict[str, object] | None = None
    usage: ModelUsage
    evidence_id: str | None = None


class ModelClient(Protocol):
    async def complete_json(
        self,
        *,
        task_id: str,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        ...
```

### 11.2 OpenAIModelClient

OpenAI async 调用必须使用 `httpx.AsyncClient`，不能 `await httpx.post(...)`。

```python
class OpenAIModelClient:
    def __init__(
        self,
        config: ModelConfig,
        cost_guard: CostGuard,
        pricing: PricingTable,
        repo: RepositoryProtocol,
    ):
        self.config = config
        self.cost_guard = cost_guard
        self.pricing = pricing
        self.repo = repo
        self.api_key = os.environ[config.api_key_env]
        self.base_url = config.base_url or "https://api.openai.com/v1"

    async def complete_json(
        self,
        *,
        task_id: str,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse:
        self.cost_guard.assert_within_budget(task_id)

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.config.model_name,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": self.config.temperature,
                    "response_format": {"type": "json_object"},
                },
            )

        if response.status_code in {401, 403}:
            raise ModelAuthError("auth_error: openai credentials invalid or unauthorized")

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise ModelRateLimitError("rate_limit", retry_after=retry_after)

        if response.status_code >= 500:
            raise ModelCallError("provider_server_error", retryable=True)

        response.raise_for_status()
        data = response.json()

        usage_raw = data.get("usage", {})
        input_tokens = int(usage_raw.get("prompt_tokens", 0))
        output_tokens = int(usage_raw.get("completion_tokens", 0))
        cost_usd = self.pricing.estimate(
            self.config.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

        self.cost_guard.record_llm_usage(task_id, usage)

        evidence_id = self.repo.save_model_call_as_evidence(
            task_id=task_id,
            agent_id=agent_id,
            provider=self.config.provider.value,
            model=self.config.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            response_preview=str(data)[:5000],
        )

        content = data["choices"][0]["message"]["content"]
        return ModelResponse(
            content=content,
            json_data=json.loads(content),
            usage=usage,
            evidence_id=evidence_id,
        )
```

### 11.3 PricingTable

```python
class PricingTable:
    """
    Prices are per 1M tokens.
    Values should be reviewed before production use.
    Unknown model returns 0.0 but emits an event.
    """

    PRICES = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 5.00, "output": 15.00},
        "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00},
    }

    def __init__(self, repo: RepositoryProtocol):
        self.repo = repo

    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> float:
        if model not in self.PRICES:
            self.repo.append_event(
                "unknown_model_pricing",
                {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens},
            )
            return 0.0

        price = self.PRICES[model]
        return (
            input_tokens / 1_000_000 * price["input"]
            + output_tokens / 1_000_000 * price["output"]
        )
```

未知模型默认 0.0 是为了不阻断本地测试，但必须记录 `unknown_model_pricing` 事件。生产环境可以配置 `fail_on_unknown_model_pricing=True`。

### 11.4 LLM 错误恢复

最低恢复策略：

```text
1. 401/403 → ModelAuthError → WorkerResult.requires_human=True → HUMAN_REQUIRED。
2. 429 → 尊重 Retry-After。
3. 5xx / network timeout → 指数退避 + jitter。
4. 最大重试次数来自 BudgetAllocation.max_model_retries。
5. 超出重试次数后，该 loop 计入 no-progress，不直接 BLOCKED 整个 task。
6. 最后一次错误写入 evidence: EvidenceType.MODEL_ERROR。
```

```python
class ModelCallError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ModelAuthError(ModelCallError):
    pass


class ModelRateLimitError(ModelCallError):
    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message, retryable=True)
        self.retry_after = retry_after
```

### 11.5 DummyModelClient

v0.5a 测试必须不依赖网络。

```python
class DummyModelClient:
    async def complete_json(self, *, task_id, agent_id, messages, max_tokens):
        return ModelResponse(
            content=json.dumps({
                "summary": "dummy response",
                "actions": [],
            }),
            json_data={"summary": "dummy response", "actions": []},
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        )
```

---

## 12. Orchestrator

### 12.1 step() 职责

Orchestrator 是 v0.5a 的核心。它必须：

```text
1. CLI resume preflight 之后开始。
2. 读取 policy / clock / ledger。
3. tick hunger。
4. 若 should_stop，返回 StopReport。
5. 生成 loop_id。
6. 立即消耗 loop_count。
7. 创建 candidate workspace。
8. 分配 budget。
9. 调用 planner。
10. 空 plan 走 no-progress，不直接 BLOCKED。
11. 调用 WorkerRuntime。
12. 处理 requires_human / safety stop / error。
13. Integrator 生成 CandidateState。
14. ValidationGate targeted validate。
15. CommitManager promote/reject。
16. HungerUpdate。
17. StagnationDetector。
18. MemoryManager maybe propose。
19. SkillManager maybe create skill card。
20. 保存 LoopTrace。
```

### 12.2 完整伪代码

```python
async def step(self, task_id: str) -> LoopTrace | StopReport:
    policy = repo.get_hunger_policy(task_id)
    clock = repo.get_hunger_clock(task_id)
    ledger = repo.get_hunger_ledger(task_id)
    previous_phase = repo.get_last_phase(task_id)

    snapshot = hunger_engine.tick(policy, clock, ledger, previous_phase=previous_phase)
    repo.save_hunger_snapshot(task_id, snapshot)

    if snapshot.should_stop:
        return build_stop_report(task_id, snapshot.stop_reason)

    loop_id = repo.next_loop_id(task_id)

    # Consume loop budget as soon as the loop is accepted for execution.
    clock.loop_count += 1
    repo.save_hunger_clock(clock)

    candidate_root = workspace_manager.create_candidate_workspace(task_id, loop_id)
    usage_before = repo.get_usage_snapshot(task_id)

    budget = budget_allocator.allocate(snapshot)
    plan = planner.plan(task_id, loop_id, snapshot, budget)
    repo.save_loop_plan(plan)

    if not plan.assignments:
        workspace_manager.reject_candidate(task_id, loop_id)
        streak = repo.increment_no_progress_streak(task_id)
        trace = build_empty_plan_trace(...)
        repo.save_loop_trace(trace)
        if streak >= policy.max_global_no_progress_loops:
            return build_stop_report(task_id, StopReason.BLOCKED)
        return trace

    worker_results = []
    try:
        for assignment in plan.assignments:
            spec = repo.get_agent_spec(assignment.agent_id)
            context = context_builder.build_for_agent(
                task_id=task_id,
                loop_id=loop_id,
                agent_id=assignment.agent_id,
                mission=assignment.mission,
                target_hunger_item_ids=assignment.target_hunger_item_ids,
                budget=budget,
                allowed_tools=assignment.allowed_tools,
                output_schema_name=spec.output_schema_name,
                candidate_workspace_ref=f"candidates/loop_{loop_id:03d}",
            )
            result = await worker_runtime.run(spec, context, workspace_root=candidate_root)
            repo.save_worker_result(result)
            worker_results.append(result)

    except SafetyStopError as exc:
        workspace_manager.reject_candidate(task_id, loop_id)
        evidence_id = repo.save_model_error_as_evidence(...)
        return build_stop_report(task_id, StopReason.SAFETY_STOP)

    if any(r.requires_human for r in worker_results):
        workspace_manager.reject_candidate(task_id, loop_id)
        return build_stop_report(task_id, StopReason.HUMAN_REQUIRED)

    candidate = integrator.integrate(task_id, loop_id, worker_results)
    repo.save_candidate(candidate)

    validation = await validation_gate.validate(
        task_id=task_id,
        loop_id=loop_id,
        candidate=candidate,
        target_hunger_item_ids=plan.selected_hunger_item_ids,
    )
    repo.save_validation_report(validation)

    commit_result = commit_manager.apply(candidate, validation)
    hunger_update.apply_validation(task_id, validation)
    stagnation = stagnation_detector.update(task_id, loop_id, validation)

    memory_manager.propose_from_loop(task_id, loop_id, validation)

    usage_after = repo.get_usage_snapshot(task_id)
    trace = LoopTrace(
        task_id=task_id,
        loop_id=loop_id,
        phase=budget.phase.value,
        active_hunger=snapshot.active_hunger,
        drive_budget=snapshot.drive_budget,
        work_pressure=snapshot.work_pressure,
        selected_hunger_item_ids=plan.selected_hunger_item_ids,
        worker_ids=[a.agent_id for a in plan.assignments],
        candidate_state_id=candidate.id,
        validation_report_id=validation.id,
        committed=commit_result["committed"],
        delta_summary=build_delta_summary(validation, commit_result),
        blocked_item_ids=stagnation["blocked_items"],
        tokens_consumed_this_loop=usage_after.tokens - usage_before.tokens,
        cost_this_loop_usd=usage_after.cost_usd - usage_before.cost_usd,
        llm_calls=usage_after.llm_calls - usage_before.llm_calls,
        next_action="continue",
    )
    repo.save_loop_trace(trace)

    if stagnation["global_blocked"]:
        return build_stop_report(task_id, StopReason.BLOCKED)

    return trace
```

### 12.3 StopReason 处理

Orchestrator 必须能输出完整 StopReport：

```text
DONE
HUNGER_EXPIRED
BLOCKED
HUMAN_REQUIRED
HUMAN_PAUSED
SAFETY_STOP
ERROR
```

---

## 13. LoopTrace 与 StopReport 扩展

### 13.1 LoopTrace

```python
class LoopTrace(BaseModel):
    task_id: str
    loop_id: int
    phase: str

    active_hunger: float
    drive_budget: float
    work_pressure: float

    selected_hunger_item_ids: list[str] = Field(default_factory=list)
    worker_ids: list[str] = Field(default_factory=list)

    candidate_state_id: str | None = None
    validation_report_id: str | None = None
    committed: bool = False

    newly_passed_check_keys: list[str] = Field(default_factory=list)
    regressed_check_keys: list[str] = Field(default_factory=list)
    blocked_items_added: list[str] = Field(default_factory=list)

    tokens_consumed_this_loop: int = 0
    cost_this_loop_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0

    stop_reason: StopReason | None = None
    delta_summary: str = ""
    next_action: str = "continue"
```

### 13.2 StopReport

```python
class StopReport(BaseModel):
    task_id: str
    stop_reason: StopReason
    goal_status: str

    final_best_state_id: str | None = None
    best_state_summary: str | None = None
    accepted_check_keys_count: int = 0

    total_loops: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0

    remaining_hunger_items: list[str] = Field(default_factory=list)
    blocked_hunger_items: list[str] = Field(default_factory=list)

    recommended_refill: float | None = None
    recommendation: str = ""
```

---

## 14. HungerUpdate 浮点收敛修正

### 14.1 问题

按 `new_count / total_checks` 多轮扣减可能留下 `1e-17` 残余，导致 `gap_score == 0.0` 永远不成立。

### 14.2 规则

```text
1. 若 item.id in report.satisfied_hunger_item_ids，则直接 gap_score = 0.0。
2. 其它部分进展使用 max(0.0, gap_score - decrement)。
3. 保存前若 gap_score <= EPSILON，则 snap 到 0.0。
4. EPSILON = 1e-9。
```

### 14.3 伪代码

```python
EPSILON = 1e-9

if item.id in report.satisfied_hunger_item_ids:
    item.gap_score = 0.0
else:
    item.gap_score = max(0.0, item.gap_score - decrement)
    if item.gap_score <= EPSILON:
        item.gap_score = 0.0

if item.gap_score == 0.0:
    item.status = HungerItemStatus.VALIDATED_SATISFIED
```

---

## 15. BLOCKED 与 human unblock

### 15.1 refill 不自动 unblock

补充 hunger 只增加 drive budget，不改变 item 的 BLOCKED 状态。

原因：BLOCKED 表示策略、权限、资源或验收路径有问题，仅补充预算不能解决。

### 15.2 unblock 命令

新增 CLI：

```bash
hungerloop hunger unblock <task_id> <item_id>
hungerloop hunger unblock-all <task_id>
```

行为：

```python
item.status = HungerItemStatus.OPEN
item.consecutive_failure_count = 0
item.last_progress_loop_id = None
repo.save_hunger_item(item)
repo.append_event("human_unblocked_hunger_item", {...})
```

---

## 16. RepositoryProtocol

### 16.1 Definition of Done

v0.5a 的 DoD 之一：核心 services 不再使用 `repo: Any`。至少以下类必须依赖 `RepositoryProtocol`：

```text
CommitManager
CostGuard
ContextBuilder
SandboxRunner
ValidationGate
WorkerRuntime
LoopOrchestrator
ToolHarness
MemoryManager
SkillManager
```

### 16.2 现有方法保持

保留 v0.4.1 中已有的方法：

```python
class RepositoryProtocol(Protocol):
    def get_best_state(self, task_id: str) -> BestState | None: ...
    def save_best_state(self, best: BestState) -> None: ...

    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]: ...
    def get_hunger_item(self, item_id: str) -> HungerItem | None: ...
    def save_hunger_item(self, item: HungerItem) -> None: ...
    def get_items_for_check_keys(self, task_id: str, check_keys: list[str]) -> list[HungerItem]: ...

    def get_hunger_policy(self, task_id: str) -> HungerPolicy: ...
    def get_hunger_clock(self, task_id: str) -> HungerClockState: ...
    def save_hunger_clock(self, clock: HungerClockState) -> None: ...
    def get_hunger_ledger(self, task_id: str) -> HungerLedger: ...
    def get_last_phase(self, task_id: str) -> LoopPhase | None: ...

    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None: ...

    def save_candidate(self, candidate: CandidateState) -> None: ...
    def mark_candidate_committed(self, candidate_id: str) -> None: ...
    def mark_candidate_rejected(self, candidate_id: str) -> None: ...

    def save_validation_report(self, report: ValidationReport) -> None: ...
    def add_failure_from_validation(self, report: ValidationReport) -> None: ...

    def count_evidence_by_type(self, task_id: str, evidence_ids: list[str], evidence_type: EvidenceType) -> int: ...
    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[Artifact]: ...
    def is_approval_granted(self, approval_id: str) -> bool: ...

    def save_shell_output_as_evidence(...) -> str: ...
    def reset_no_progress_streak(self, task_id: str) -> None: ...
    def increment_no_progress_streak(self, task_id: str) -> int: ...
    def next_loop_id(self, task_id: str) -> int: ...
    def append_event(self, event_type: str, payload: dict[str, object]) -> None: ...
```

### 16.3 新增 Repository 方法清单

v0.5.2 必须集中新增以下方法，不允许散落在实现里但缺 Protocol：

```python
class RepositoryProtocol(Protocol):
    # Planning / worker
    def save_loop_plan(self, plan: LoopPlan) -> None: ...
    def get_agent_spec(self, agent_id: str) -> AgentSpec: ...
    def save_agent_spec(self, spec: AgentSpec) -> None: ...
    def save_worker_result(self, result: WorkerResult) -> None: ...

    # Usage / trace
    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot: ...
    def save_loop_trace(self, trace: LoopTrace) -> None: ...
    def get_last_stop_reason(self, task_id: str) -> StopReason | None: ...
    def save_stop_report(self, report: StopReport) -> None: ...

    # Model / evidence
    def save_model_call_as_evidence(
        self,
        *,
        task_id: str,
        agent_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        response_preview: str,
    ) -> str: ...

    def save_model_error_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        provider: str,
        model: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> str: ...

    # Memory / Skill
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None: ...
    def list_memory_candidates(self, task_id: str) -> list[MemoryCandidate]: ...
    def count_committed_references(self, candidate_id: str) -> int: ...
    def save_skill_card(self, card: SkillCard) -> None: ...
    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]: ...
```

### 16.4 UsageSnapshot

```python
class UsageSnapshot(BaseModel):
    task_id: str
    tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
```

---

## 17. SQLiteRepository 与 Schema

### 17.1 持久化范围

v0.5a 必须提供 SQLiteRepository。  
InMemoryRepository 继续用于测试，但 CLI 默认使用 SQLite。

### 17.2 必需表

```sql
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,
  last_stop_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE hunger_policies (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE hunger_clocks (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE hunger_items (
  item_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  gap_score REAL NOT NULL,
  priority REAL NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE best_states (
  task_id TEXT PRIMARY KEY,
  state_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE candidates (
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE validation_reports (
  validation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE accepted_checks (
  task_id TEXT NOT NULL,
  check_key TEXT NOT NULL,
  hunger_item_id TEXT NOT NULL,
  check_index INTEGER NOT NULL,
  accepted_at_loop INTEGER NOT NULL,
  validation_id TEXT NOT NULL,
  evidence_id TEXT,
  PRIMARY KEY (task_id, check_key)
);

CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER,
  evidence_type TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE model_errors (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  error_type TEXT NOT NULL,
  retryable INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE agent_specs (
  agent_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE worker_results (
  result_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE loop_plans (
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, loop_id)
);

CREATE TABLE loop_traces (
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, loop_id)
);

CREATE TABLE stop_reports (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE no_progress_streak (
  task_id TEXT PRIMARY KEY,
  streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE memory_candidates (
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE skill_cards (
  skill_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  task_id TEXT,
  loop_id INTEGER,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

### 17.3 Storage rules

```text
1. SQLite stores metadata and JSON payloads.
2. Workspaces remain filesystem directories.
3. Model responses larger than 5000 chars are stored as files and referenced by evidence URI.
4. CLI defaults to workspace/tasks/<task_id>/blackboard.sqlite.
```

### 17.4 EvidenceType 枚举使用

All evidence insertions must validate `evidence_type` against:

```python
EVIDENCE_TYPES = {
    "sandbox_run",
    "model_call",
    "model_error",
    "validation_check",
    "tool_call",
    "human_input",
}
```

---

## 18. CLI

### 18.1 new

```bash
hungerloop new "Fix failing test" \
  --policy examples/demo_policy.yaml \
  --accept 'shell_exit_zero:argv=["pytest","tests/test_foo.py"]:timeout=60'
```

Creates:

```text
workspace/tasks/<task_id>/blackboard.sqlite
workspace/tasks/<task_id>/best/files/
workspace/tasks/<task_id>/events.jsonl
```

### 18.2 run

```bash
hungerloop run <task_id> --model-config model_config.yaml --max-loops 20
```

Rules:

```text
1. If task has no previous stop_reason, run normally.
2. If last_stop_reason exists, require --resume or --reset.
3. --resume continues without resetting cost or loop_count.
4. --reset requires explicit confirmation and creates a new task version.
```

### 18.3 resume preflight

CLI must pre-check `last_stop_reason` before calling Orchestrator.

```text
if last_stop_reason == HUNGER_EXPIRED:
    require prior hunger refill or --refill option

if last_stop_reason == BLOCKED:
    require hungerloop hunger unblock or --unblock-all

if last_stop_reason == HUMAN_REQUIRED:
    require the missing auth/approval/input to be resolved

if last_stop_reason == SAFETY_STOP:
    require higher max_total_cost_usd or --raise-cost-ceiling
```

If precondition is missing, CLI prints an actionable error and exits without invoking Orchestrator.

### 18.4 status

```bash
hungerloop status <task_id>
```

Displays:

```text
stop_reason
loop_count
current_drive_budget
total_cost_usd
total_tokens
best_state_id
accepted_check_keys_count
open hunger items
blocked hunger items
```

### 18.5 hunger operations

```bash
hungerloop hunger refill <task_id> --amount 50
hungerloop hunger unblock <task_id> H-001
hungerloop hunger unblock-all <task_id>
hungerloop hunger freeze <task_id>
hungerloop hunger resume <task_id>
```

---

## 19. MemoryManager

### 19.1 MemoryCandidate

```python
class MemoryCandidate(BaseModel):
    candidate_id: str
    task_id: str
    content: str
    memory_type: str
    evidence_ids: list[str] = Field(default_factory=list)
    source_candidate_id: str | None = None
    reusable: bool = False
    non_volatile: bool = False
    traceable: bool = False
    action_verified: bool = False
    status: str = "candidate"
```

### 19.2 Promotion predicates

v0.5c 只生成 candidates，不自动 promotion。但 predicates 必须可测试。

```text
action_verified:
  至少一个 evidence_id 出现在 best.evidence_ids 中，或来自 accepted_check_keys 对应 validation。

reusable:
  candidate.content 不包含 task-specific path、task_id、candidate_id、loop_id。

non_volatile:
  repo.count_committed_references(candidate_id) >= 2。

traceable:
  set(candidate.evidence_ids) ⊆ set(best.evidence_ids)。
```

### 19.3 propose_from_loop

```python
class MemoryManager:
    def propose_from_loop(self, task_id: str, loop_id: int, validation: ValidationReport) -> list[MemoryCandidate]:
        if not validation.newly_passed_check_keys:
            return []
        ...
```

---

## 20. SkillCard

### 20.1 Model

```python
class SkillCard(BaseModel):
    skill_id: str
    task_id: str
    name: str
    trigger_signals: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    accepted_check_keys: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
```

### 20.2 Trigger rule

SkillCard generation must not be vague.

v0.5c fixed rule:

```text
Generate SkillCard only when:
  stop_reason == DONE
  and best is not None
  and len(best.accepted_check_keys) >= 2
```

### 20.3 maybe_create_skill_card

```python
class SkillManager:
    def maybe_create_skill_card(
        self,
        task_id: str,
        stop_report: StopReport,
    ) -> SkillCard | None:
        best = repo.get_best_state(task_id)
        if stop_report.stop_reason != StopReason.DONE:
            return None
        if best is None or len(best.accepted_check_keys) < 2:
            return None
        card = SkillCard(...)
        repo.save_skill_card(card)
        return card
```

### 20.4 Demo requirement

`examples/demo_task.yaml` must define a deterministic task that reaches DONE with at least 2 accepted checks, so one SkillCard is generated.

---

## 21. examples/demo_task.yaml

```yaml
goal: "Create a small report and validate it."
policy:
  initial_hunger: 100
  h_max: 100
  decay_type: loop_count
  decay_duration_seconds: 8
  max_total_cost_usd: 1.0
  max_total_tokens: 100000
model:
  provider: dummy
  model_name: dummy
acceptance:
  core_acceptance_checks:
    - check_type: file_exists
      params:
        path: report.md
      description: report.md exists
    - check_type: shell_exit_zero
      params:
        argv: ["python", "-c", "open('report.md').read(); print('ok')"]
        timeout: 10
      description: report.md is readable
```

Expected deterministic output:

```text
StopReason: DONE
accepted_check_keys_count >= 2
MemoryCandidate count >= 1
SkillCard count == 1
```

---

## 22. Acceptance Criteria

### 22.1 v0.5a

```text
1. hungerloop new creates task state in SQLite.
2. hungerloop run can run with DummyModelClient.
3. Orchestrator consumes clock.loop_count each accepted loop.
4. Empty plan does not immediately BLOCK task.
5. LoopTrace records tokens/cost/llm_calls/tool_calls fields.
6. StopReport supports all 7 StopReason values.
7. ContextPack.budget is BudgetAllocation, not dict.
8. RepositoryProtocol includes all methods used by Orchestrator.
9. CLI --resume preflight blocks invalid resume attempts.
10. Tests pass without network.
```

### 22.2 v0.5b

```text
1. OpenAIModelClient works with api_key_env.
2. Literal api_key in YAML is rejected.
3. Azure provider in v0.5a raises NotImplementedError.
4. PricingTable estimates known models.
5. Unknown model pricing emits event.
6. 401/403 becomes HUMAN_REQUIRED.
7. 429 respects Retry-After.
8. LLM errors are saved as model_error evidence.
9. ExecutionWorker can use LLM + ToolHarness to create/patch files.
10. Worker shell calls use SandboxRunner only.
```

### 22.3 v0.5c

```text
1. MemoryCandidate generated from DONE demo.
2. Predicates are deterministic and unit-tested.
3. SkillCard generated only when stop_reason == DONE and accepted checks >= 2.
4. memory list and skill list work.
```

---

## 23. Testing Plan

### 23.1 P0 unit tests

```text
test_stop_reason_schema.py
  - includes HUMAN_REQUIRED and ERROR

test_context_budget_type.py
  - ContextPack.budget is BudgetAllocation
  - context.budget.max_tokens works

test_rule_based_planner.py
  - selects max priority×gap_score item
  - routes to execution_worker_v1
  - empty active items returns empty plan without BLOCKED

test_worker_runtime.py
  - dispatches to worker by agent_id
  - missing worker returns WorkerResult.error
  - ModelAuthError returns requires_human=True
  - WorkerBudgetExceeded returns retryable=False
  - timeout returns worker_timeout

test_tool_harness.py
  - read_file path cannot escape workspace
  - write_file writes only candidate workspace
  - run_shell uses SandboxRunner argv

test_model_config_loader.py
  - literal api_key rejected
  - api_key_env missing raises ModelAuthError
  - azure_openai raises NotImplementedError in v0.5a

test_pricing_table.py
  - known model estimates cost
  - unknown model appends unknown_model_pricing event

test_gap_score_epsilon.py
  - satisfied item snaps to 0.0
  - <= EPSILON snaps to 0.0

test_resume_preflight.py
  - HUNGER_EXPIRED requires refill
  - BLOCKED requires unblock
  - SAFETY_STOP requires raised cost ceiling
```

### 23.2 Integration tests

```text
test_orchestrator_dummy_done.py
  - demo task reaches DONE with DummyModelClient

test_orchestrator_hunger_expired.py
  - loop_count budget expires and StopReport == HUNGER_EXPIRED

test_orchestrator_human_required.py
  - model auth error propagates to HUMAN_REQUIRED

test_orchestrator_safety_stop.py
  - cost ceiling triggers SAFETY_STOP immediately

test_rejected_candidate_does_not_pollute_best.py
  - candidate reject preserves best workspace

test_skill_card_trigger.py
  - DONE + >=2 checks creates SkillCard
```

---

## 24. Roadmap

### v0.5a — Orchestrator + Dummy ExecutionWorker

```text
1. StopReason schema extension.
2. RepositoryProtocol + SQLiteRepository.
3. AgentSpecRegistry with execution_worker_v1.
4. RuleBasedPlanner.
5. BudgetAllocation / BudgetGuard.
6. WorkerRuntime.
7. DummyModelClient.
8. Minimal ExecutionWorker.
9. LoopOrchestrator.
10. CLI new/run/status/resume preflight.
11. examples/demo_task.yaml.
```

### v0.5b — OpenAI + ToolHarness + minimal real execution

```text
1. ModelConfig loader.
2. OpenAIModelClient.
3. PricingTable.
4. LLM retry and model_error evidence.
5. ToolHarness.
6. ExecutionWorker can write patch + run tests.
7. CLI --model-config.
```

### v0.5c — Memory + SkillCard

```text
1. MemoryManager.
2. SkillManager.
3. MemoryCandidate deterministic predicates.
4. SkillCard trigger.
5. memory / skill CLI.
```

### v0.5d — Additional workers, still no LLMPlanner

```text
1. Add simple LearningWorker.
2. Add simple ResearchWorker.
3. Keep RuleBasedPlanner.
4. Validate context isolation.
```

### v0.6 — LLMPlanner and multi-worker

```text
1. LLMPlanner.
2. Multi-worker assignments.
3. Optional parallel execution.
4. More advanced memory retrieval.
```

---

## 25. Non-goals

```text
1. No FastAPI in v0.5.
2. No browser automation.
3. No desktop automation.
4. No LLM-as-judge.
5. No automatic Memory Promotion.
6. No Azure OpenAI runtime in v0.5a.
7. No 3×3 agents in v0.5a/b/c.
8. No model routing.
9. No background daemon.
10. No unbounded autonomous execution.
```

---

## 26. Implementation order

```text
Day 1:
  StopReason schema, EvidenceType, BudgetAllocation, ContextPack.budget migration.

Day 2:
  RepositoryProtocol updates and SQLite schema.

Day 3:
  AgentSpecRegistry and RuleBasedPlanner.

Day 4:
  WorkerRuntime and DummyModelClient.

Day 5:
  Minimal ExecutionWorker and ToolHarness.

Day 6:
  LoopOrchestrator.step and run.

Day 7:
  CLI new/run/status/resume preflight.

Day 8:
  OpenAI ModelConfig loader and PricingTable.

Day 9:
  OpenAIModelClient and LLM retry/error evidence.

Day 10:
  demo_task integration and gap_score epsilon fix.

Day 11:
  MemoryManager predicates.

Day 12:
  SkillCard trigger and CLI list commands.

Day 13:
  integration tests.

Day 14:
  docs, README, release checklist.
```

---

## 27. Final definition of done

v0.5.2 这份 PRD 通过的标准：

```text
1. 每个 Orchestrator 调用到的方法都在 RepositoryProtocol 中。
2. 每个 Protocol 方法都在 SQLite schema 中有落点。
3. 每个 worker 错误都有明确 stop/error 映射。
4. 每个 model call 都带 task_id。
5. 每个 LLM/tool 调用都受 task ceiling 和 worker budget 双层约束。
6. 每个 shell 调用都经过 ToolHarness + SandboxRunner。
7. 每个 path 都经过 path_safety。
8. 每个 run 都能 resume 或给出 actionable preflight error。
9. 每个 STOP 都输出完整 StopReport。
10. 每个 DONE demo 都能生成确定性 SkillCard。
```

一句话总结：

> v0.5.2 的目标不是更聪明，而是让 HungerLoop 第一次真正具备“可运行、可恢复、可记账、可验证、可停止”的 agent loop 闭环。

---

## 28. 三轮 review 修订（M 系列）

**说明**：本节是对 §1–§27 主体规格的增量修订，按 review 反馈编号。每条均给出 (a) 问题描述，(b) 影响范围，(c) 具体修法。实现以本节为准；与上文冲突时本节优先。

修订项分两档：

```text
🔴 P0  v0.5a 启动前必修，否则跑不起来或 demo 端到端失败。
🟡 P1  v0.5a 编码前清掉，避免协议/schema 内部不一致。
🟢 P2  实现时顺手处理。
```

---

### 28.1 🔴 M21 — DummyModelClient 必须可脚本化（demo 端到端能跑通的唯一卡点）

**问题**：§11.5 写死 `actions: []`；§21 demo 的 acceptance 是 `file_exists: report.md`。Worker 不产 actions → 文件不存在 → check FAIL → 永远到不了 DONE。直接违反 §22.1 #10、§22.3 #1、§24 v0.5c DoD。

**修法**：DummyModelClient 改为接受脚本响应序列，按调用顺序消费。

```python
class DummyModelClient:
    """Deterministic dummy client for tests and demos.

    Construct with a list of scripted responses. Each call to ``complete_json``
    pops the next response. If the script is exhausted, returns an empty
    response (summary='dummy fallback', actions=[]).
    """

    def __init__(self, scripted_responses: list[ModelResponse] | None = None):
        self._script: list[ModelResponse] = list(scripted_responses or [])
        self._calls: int = 0

    @classmethod
    def with_actions(cls, actions: list[dict[str, object]]) -> "DummyModelClient":
        """Convenience constructor: one response that emits the given actions."""
        payload = {"summary": "scripted dummy response", "actions": actions}
        return cls(
            [
                ModelResponse(
                    content=json.dumps(payload),
                    json_data=payload,
                    usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
                )
            ]
        )

    async def complete_json(
        self, *, task_id: str, agent_id: str,
        messages: list[dict[str, str]], max_tokens: int,
    ) -> ModelResponse:
        self._calls += 1
        if self._script:
            return self._script.pop(0)
        empty = {"summary": "dummy fallback", "actions": []}
        return ModelResponse(
            content=json.dumps(empty),
            json_data=empty,
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        )
```

**Demo 注入**（更新 §21 加载逻辑）：

```python
demo_actions = [
    {"tool_name": "write_file",
     "args": {"path": "report.md", "content": "# demo report\nok\n"}},
]
model_client = DummyModelClient.with_actions(demo_actions)
```

**测试要求**：
- v0.5a `examples/demo_task.yaml` 配套提供 `examples/demo_dummy_script.py`，CLI 通过 `--dummy-script <path>` 加载。
- DummyModelClient 不允许在生产 ModelConfigLoader 路径里被注入 scripted_responses（仅供测试/demo 入口使用）。

---

### 28.2 🔴 M6 — ModelClient 重试循环必须落到代码里

**问题**：§11.4 写了重试策略但 §11.2 `complete_json` 没有 retry 循环；§7.3 WorkerRuntime 也只 catch 一次。结果 `BudgetAllocation.max_model_retries / retry_base_delay_seconds / retry_max_delay_seconds` 全是死字段。

**修法**：在 `complete_json` 内部包重试循环，仅对可重试错误重试，尊重 `Retry-After`。重试次数从 `BudgetAllocation` 经调用方传入（避免 ModelClient 反向依赖 BudgetAllocation）。

```python
class ModelClient(Protocol):
    async def complete_json(
        self,
        *,
        task_id: str,
        agent_id: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        max_retries: int = 0,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 20.0,
    ) -> ModelResponse: ...
```

OpenAIModelClient `complete_json` 主体改造：

```python
async def complete_json(self, *, task_id, agent_id, messages, max_tokens,
                       max_retries=0, retry_base_delay_seconds=1.0,
                       retry_max_delay_seconds=20.0):
    self.cost_guard.assert_within_budget(task_id)

    last_error: ModelCallError | None = None
    async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
        for attempt in range(max_retries + 1):
            try:
                return await self._call_once(
                    client, task_id, agent_id, messages, max_tokens
                )
            except ModelRateLimitError as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                delay = self._delay_for_rate_limit(
                    exc.retry_after, attempt,
                    retry_base_delay_seconds, retry_max_delay_seconds,
                )
                await asyncio.sleep(delay)
            except ModelCallError as exc:
                last_error = exc
                if not exc.retryable or attempt >= max_retries:
                    break
                delay = min(
                    retry_max_delay_seconds,
                    retry_base_delay_seconds * (2 ** attempt) + random.uniform(0, 0.5),
                )
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error

@staticmethod
def _delay_for_rate_limit(retry_after, attempt, base, cap):
    if retry_after:
        try:
            return min(cap, float(retry_after))
        except ValueError:
            pass
    return min(cap, base * (2 ** attempt) + random.uniform(0, 0.5))
```

**WorkerRuntime / ExecutionWorker 调用约定**：调用 `complete_json` 时必须从 `context.budget` 读出三个重试参数透传。

```python
response = await self.model_client.complete_json(
    task_id=context.task_id,
    agent_id=context.agent_id,
    messages=messages,
    max_tokens=context.budget.max_tokens,
    max_retries=context.budget.max_model_retries,
    retry_base_delay_seconds=context.budget.retry_base_delay_seconds,
    retry_max_delay_seconds=context.budget.retry_max_delay_seconds,
)
```

**ModelAuthError 不重试**：`ModelAuthError` 继承 `ModelCallError` 但 `retryable=False`，循环立即跳出。

---

### 28.3 🔴 M7 — JSON 解析必须包 try/except

**问题**：§11.2 `json.loads(content)` 未保护。`response_format={"type":"json_object"}` 不 100% 保证（截断/空字符串都会抛 `JSONDecodeError`），未捕获会冒泡到 §12.2 的 `except Exception` 把整任务变成 `StopReason.ERROR`。

**修法**：包成 `ModelCallError`，标记不可重试。

```python
try:
    json_data = json.loads(content)
except json.JSONDecodeError as exc:
    raise ModelCallError(
        f"invalid_json_response: {exc.msg}",
        retryable=False,
    ) from exc
return ModelResponse(content=content, json_data=json_data, usage=usage,
                     evidence_id=evidence_id)
```

WorkerRuntime 已能 catch `ModelCallError`（§7.3），落入 `WorkerResult.error_type='model_call_error'`，由 stagnation 统计，不会一次性 BLOCK 任务。

---

### 28.4 🔴 M12 — BudgetGuard 必须有状态，否则形同虚设

**问题**：§4.4 是无状态的"估值检查"，WorkerRuntime 用默认 0 调一次永远 pass，phase budget 完全不生效。

**修法**：BudgetGuard 内部维护 (task_id, loop_id, agent_id) → UsageCounter，由 ModelClient/ToolHarness 在每次调用**完成后** `record(...)`，下一次 `assert(...)` 检查累计值是否会越界。

```python
class BudgetUsage(BaseModel):
    tokens: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    elapsed_seconds: float = 0.0


class BudgetGuard:
    def __init__(self) -> None:
        self._usage: dict[tuple[str, int, str], BudgetUsage] = {}

    def reset(self, task_id: str, loop_id: int, agent_id: str) -> None:
        self._usage.pop((task_id, loop_id, agent_id), None)

    def record(self, task_id: str, loop_id: int, agent_id: str,
               *, tokens: int = 0, tool_calls: int = 0,
               llm_calls: int = 0, elapsed_seconds: float = 0.0) -> None:
        key = (task_id, loop_id, agent_id)
        cur = self._usage.get(key, BudgetUsage())
        cur.tokens += tokens
        cur.tool_calls += tool_calls
        cur.llm_calls += llm_calls
        cur.elapsed_seconds += elapsed_seconds
        self._usage[key] = cur

    def assert_can_spend(
        self, context: ContextPack,
        *, addl_tokens: int = 0, addl_tool_calls: int = 0,
        addl_llm_calls: int = 0,
    ) -> None:
        key = (context.task_id, context.loop_id, context.agent_id)
        cur = self._usage.get(key, BudgetUsage())
        if cur.tokens + addl_tokens > context.budget.max_tokens:
            raise WorkerBudgetExceeded(
                f"token budget exceeded: {cur.tokens}+{addl_tokens} > {context.budget.max_tokens}"
            )
        if cur.tool_calls + addl_tool_calls > context.budget.max_tool_calls:
            raise WorkerBudgetExceeded("tool_call budget exceeded")
        if cur.llm_calls + addl_llm_calls > 999_999:  # llm_calls 单独成字段时再加
            raise WorkerBudgetExceeded("llm_call budget exceeded")
```

**调用契约**：

```text
1. WorkerRuntime.run 入口：budget_guard.reset(task_id, loop_id, agent_id)。
2. ModelClient 调用前：budget_guard.assert_can_spend(context, addl_llm_calls=1)。
3. ModelClient 调用后：budget_guard.record(..., tokens=usage.input_tokens+usage.output_tokens, llm_calls=1)。
4. ToolHarness.execute 前：budget_guard.assert_can_spend(context, addl_tool_calls=1)。
5. ToolHarness.execute 后：budget_guard.record(..., tool_calls=1)。
```

`assert_worker_budget` 旧签名废弃；§7.3 WorkerRuntime 伪代码改用 `reset` + 由下游各 service 自检 / 自录。

---

### 28.5 🔴 M18 — 补 `save_tool_call_as_evidence` 协议方法

**问题**：§9.1 要求 ToolHarness 产生 tool_call evidence，但 §16.3 没对应方法。

**修法**：§16.3 末尾增补：

```python
def save_tool_call_as_evidence(
    self,
    *,
    task_id: str,
    loop_id: int,
    agent_id: str,
    tool_name: str,
    args_summary: str,         # truncated to 2000 chars
    result_summary: str,       # truncated to 2000 chars
    success: bool,
    elapsed_ms: int,
) -> str: ...
```

落表：复用 `evidence` 表，`evidence_type = 'tool_call'`，`payload_json` 含上述字段。

---

### 28.6 🔴 M20 — `StopReport.goal_status` 改为 Literal 并定映射

**问题**：§13.2 字段是裸 `str`，CLI 与测试无法对齐。

**修法**：

```python
GoalStatus = Literal["completed", "partial", "blocked", "abandoned", "paused"]

class StopReport(BaseModel):
    ...
    goal_status: GoalStatus
    ...
```

stop_reason → goal_status 强制映射表（在 `build_stop_report` 中实现）：

| stop_reason | goal_status |
|---|---|
| DONE | completed |
| HUNGER_EXPIRED | partial（若 best 非空且 accepted_check_keys 非空）/ abandoned（否则） |
| BLOCKED | blocked |
| HUMAN_REQUIRED | paused |
| HUMAN_PAUSED | paused |
| SAFETY_STOP | abandoned |
| ERROR | abandoned |

---

### 28.7 🟡 M1 — ContextBuilder 同步改造

§4.3 把 `ContextPack.budget` 升级为 `BudgetAllocation` 后，`services/context_builder.py:67` 当前构造 dict 的代码必须改成直接传入实例：

```python
return ContextPack(
    ...
    budget=budget,   # 直接传 BudgetAllocation；删除 dict 构造
    ...
)
```

并在 `build_for_agent` 签名上把 `budget: BudgetAllocation` 写明（已是当前签名 ✓，但 dict 转换要删）。

---

### 28.8 🟡 M2 — AgentSpec 扩展 `kind` 字段（schema 变更）

补到 §3 schema 变更清单：

```python
AgentKind = Literal["execution", "learning", "research", "planner"]

class AgentSpec(BaseModel):
    agent_id: str
    name: str
    kind: AgentKind = "execution"   # NEW in v0.5.2
    output_schema_name: str = "default"
    allowed_tools: list[str] = Field(default_factory=list)
```

v0.5a 唯一注册值：`kind="execution"`。

---

### 28.9 🟡 M9 — `accepted_checks` 表必须有写入路径

补 §16.3：

```python
def save_accepted_check(
    self,
    *,
    task_id: str,
    check_key: str,
    hunger_item_id: str,
    check_index: int,
    accepted_at_loop: int,
    validation_id: str,
    evidence_id: str | None,
) -> None: ...
```

调用契约：CommitManager 在 promote 成功后，对 `report.newly_passed_check_keys` 中每个 key 调用一次 `save_accepted_check`。该表用于 §19.2 `action_verified` 谓词的高效查询，避免每次反序列化 BestState.payload_json。

---

### 28.10 🟡 M10 — §16.1 必须使用 RepositoryProtocol 的清单补全

将 `HungerUpdateService` 与 `StagnationDetector` 加入 §16.1 清单。两者当前 `repo: Any`（`hunger_update.py:21`、`stagnation_detector.py:33`），v0.5a 一并收紧。

完整清单：

```text
CommitManager
CostGuard
ContextBuilder
SandboxRunner
ValidationGate
WorkerRuntime
LoopOrchestrator
ToolHarness
MemoryManager
SkillManager
HungerUpdateService     # ADDED
StagnationDetector      # ADDED
RuleBasedPlanner        # ADDED（也持有 repo）
```

---

### 28.11 🟡 M11 — BudgetAllocation 中的 `allow_*` 字段必须由 ToolHarness 强制

§4.2 字段保留，但必须在 §9 ToolHarness 增加强制点：

```python
class ToolNotPermitted(RuntimeError):
    pass

# inside ToolHarness.execute (before dispatch)
tool = self.tool_registry[tool_name]
if tool.side_effect_level == "shell" and not budget.allow_shell:
    raise ToolNotPermitted(f"shell disabled by budget: {tool_name}")
if tool.side_effect_level == "file_write" and not budget.allow_file_write:
    raise ToolNotPermitted(f"file_write disabled by budget: {tool_name}")
if tool.requires_network and not budget.allow_network:
    raise ToolNotPermitted(f"network disabled by budget: {tool_name}")
```

`ToolNotPermitted` 由 WorkerRuntime catch（同 `WorkerBudgetExceeded` 同级），落为 `WorkerResult.error_type="tool_not_permitted"`, `retryable=False`。

`BudgetAllocation.max_subagents` / `max_new_branches` 二选一统一命名：**统一用 `max_workers_per_loop`**，v0.5a 固定 1。RuleBasedPlanner §5.2 第 3 条改为 `N = min(1, budget.max_workers_per_loop)`。

---

### 28.12 🟡 M13 — `hungerloop hunger refill` 语义写死

§15 增加 §15.3：

```text
refill 语义（v0.5a）：
  amount 单位：loop budget 数量（int）。
  作用：clock.loop_count = max(0, clock.loop_count - amount)。
  不修改：consumed_tokens、consumed_by_cost_usd、policy.*。
  effect：等价于"归还 N 轮 LOOP_COUNT 预算"。
  事件：repo.append_event("hunger_refilled",
        {"task_id": ..., "amount_loops": amount,
         "before": before_loop_count, "after": after_loop_count})。
```

理由：当前 `LOOP_COUNT` 是默认 decay_type，refill 直接对它生效最直观；token/cost ceiling 走 `--raise-cost-ceiling` 这条独立路径，不混入 refill。

CLI 签名从 `--amount 50` 改为 `--loops 5`，避免单位歧义：

```bash
hungerloop hunger refill <task_id> --loops 5
```

---

### 28.13 🟡 M14 + M17 — `--reset` 用新 task_id，不复用主键

§18.2 + §17.2 修订：

```text
--reset 语义：
  1. 生成新 task_id：<original>__r<N>，N 从 1 自增。
  2. 所有持久化按新 task_id 写入；旧 task_id 数据保留不变。
  3. workspace 目录：workspace/tasks/<new_task_id>/...
  4. events 表 payload_json 中带 parent_task_id 字段，便于追溯。
  5. 不需要复合主键；所有现有表 task_id PK 不变。
  6. CLI 输出新 task_id，提示用户后续命令使用新 ID。
```

`hungerloop status <original>` 仍能查到原任务终态；`hungerloop status <original>__r1` 查 reset 后任务。

---

### 28.14 🟡 M15 — `append_event` 加 task_id / loop_id 参数

§16 修订协议：

```python
def append_event(
    self,
    event_type: str,
    payload: dict[str, object],
    *,
    task_id: str | None = None,
    loop_id: int | None = None,
) -> None: ...
```

`events` 表已有 `task_id TEXT` / `loop_id INTEGER` 列（§17.2 ✓），实现时直接落列即可。所有调用点（PricingTable、StagnationDetector、unblock CLI 等）必须传 task_id；只有真正全局事件（如启动/关闭）允许不传。

---

### 28.15 🟡 M16 — `stop_reports` 表支持多次停止历史

**修法**：保留 `task_id PRIMARY KEY` 不变，但内部 `payload_json` 字段名改为 `latest_payload_json`，并新增 `history_payload_json TEXT NOT NULL DEFAULT '[]'`（一个 JSON array，按时间追加）：

```sql
CREATE TABLE stop_reports (
  task_id TEXT PRIMARY KEY,
  latest_payload_json TEXT NOT NULL,
  history_payload_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);
```

写入逻辑：

```python
def save_stop_report(self, report: StopReport) -> None:
    existing = self._get_history(report.task_id)  # list[dict]
    existing.append(report.model_dump(mode='json'))
    # latest = report; history = existing
```

仍然单行 per task，但保留完整停止历史，配合 §28.13 的 `--reset` 新 task_id 策略已经足够；无需引入 generation 列。

---

### 28.16 🟢 M4 — StopReport 持久化职责

明确：**Orchestrator 不调用 `repo.save_stop_report`**。CLI 在拿到 StopReport 后负责持久化：

```python
report = await orchestrator.step(task_id)
if isinstance(report, StopReport):
    repo.save_stop_report(report)
    print_stop_report(report)
```

理由：单元测试可以独立验证 Orchestrator 不依赖 stop_report 持久化副作用。

---

### 28.17 🟢 M8 — `model_errors.loop_id` 改为 nullable

`save_model_error_as_evidence` 的 `loop_id` 改 `int | None`，schema 改 `loop_id INTEGER`（去掉 NOT NULL）。理由：ModelConfigLoader 阶段失败时尚无 loop_id；当前强制 NOT NULL 会让 early error 无法落证。

---

### 28.18 🟢 字段命名统一收尾

| 原字段 | 出现位置 | 统一为 |
|---|---|---|
| `budget.max_subagents` | §5.2 | `budget.max_workers_per_loop` |
| `budget.max_new_branches` | §4.2 | 删除 |
| `BudgetGuard.assert_worker_budget` | §4.4 / §7.3 | `BudgetGuard.assert_can_spend` + `record` + `reset`（见 §28.4） |

---

### 28.19 修订汇总映射

| 编号 | 档 | 题目 | 本节位置 |
|---|---|---|---|
| M21 | 🔴 | DummyModelClient 可脚本化 | §28.1 |
| M6 | 🔴 | ModelClient 重试循环 | §28.2 |
| M7 | 🔴 | JSON 解析容错 | §28.3 |
| M12 | 🔴 | BudgetGuard 有状态化 | §28.4 |
| M18 | 🔴 | save_tool_call_as_evidence | §28.5 |
| M20 | 🔴 | goal_status Literal 化 | §28.6 |
| M1 | 🟡 | ContextBuilder 改造 | §28.7 |
| M2 | 🟡 | AgentSpec.kind | §28.8 |
| M9 | 🟡 | accepted_checks 写入 | §28.9 |
| M10 | 🟡 | RepositoryProtocol 用户清单 | §28.10 |
| M11 | 🟡 | allow_* 强制 + 字段名统一 | §28.11 |
| M13 | 🟡 | refill 语义 | §28.12 |
| M14/M17 | 🟡 | --reset 新 task_id | §28.13 |
| M15 | 🟡 | append_event 参数 | §28.14 |
| M16 | 🟡 | stop_reports 历史 | §28.15 |
| M4 | 🟢 | StopReport 持久化职责 | §28.16 |
| M8 | 🟢 | model_errors loop_id nullable | §28.17 |
| 命名 | 🟢 | budget 字段统一 | §28.18 |

---

### 28.20 v0.5.2 → v0.5.2.1 DoD 增量

在 §27 既有 10 条之上新增：

```text
11. DummyModelClient 支持脚本化响应；examples/demo_task 通过 dummy 端到端跑通到 DONE。
12. ModelClient 重试循环存在且尊重 Retry-After；max_model_retries=0 时表现为单次调用。
13. JSON 解析失败转为 ModelCallError(retryable=False)，不会冒泡为 StopReason.ERROR。
14. BudgetGuard 有状态：phase 内 max_tokens / max_tool_calls 越界时 raise WorkerBudgetExceeded。
15. Tool 调用产生 evidence_type='tool_call' 的 evidence 行。
16. StopReport.goal_status 从 stop_reason 确定性映射，无散落 string。
17. 所有 services 不再使用 repo: Any（含 HungerUpdateService、StagnationDetector）。
18. --reset 生成新 task_id <original>__r<N>，原任务数据不被覆盖。
19. append_event 调用必须传 task_id 或显式标注全局事件。
20. stop_reports 表保留完整停止历史。
```

