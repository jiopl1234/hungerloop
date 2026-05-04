# HungerLoop v0.5b+c PRD Rewrite

**版本**：v0.5b+c rewrite, compatibility-first  
**日期**：2026-05-04  
**基线**：v0.5a main + post-review fixes  
**目标**：在不推倒 v0.5a API 的前提下，完成持久化、真实模型调用、最小代码任务闭环和可恢复 CLI。

---

## 0. 本版重写原则

这版 PRD 只做一件事：**把 v0.5b+c 规格改成 v0.5a 的增量演进，而不是重写 v0.5a。**

Claude review 指出的核心风险是：上一版 PRD 方向正确，但大量 API 看起来是“补充”，实际会重写 v0.5a 已经稳定的接口。若照做，会破坏大量测试、模型客户端、WorkerRuntime、ToolHarness、ExecutionWorker 和刚合并的 review fixes。

因此本版采用以下原则：

1. **保留 v0.5a 已存在且测试通过的 public contract。**
2. **新增能力优先通过兼容字段、适配层、可选参数实现。**
3. **必须破坏接口的改动，只能进入 v0.6 或 migration plan。**
4. **v0.5b 先做可恢复持久化和真实 CLI。**
5. **v0.5c 再做真实 ExecutionWorker 代码任务闭环。**
6. **多 worker、复杂 planner、自动 memory promotion 不进 v0.5b/c。**

---

## 1. Release 拆分

v0.5b+c 不再作为一个大包交付。拆成四个小版本。

| 版本 | 主题 | 目标 |
|---|---|---|
| v0.5b.0 | SQLite + CLI dummy | 可创建、运行、恢复 dummy task |
| v0.5b.1 | ModelConfig + OpenAI | 真实模型调用进入 loop |
| v0.5c.0 | ExecutionWorker + tools | 跑通最小 pytest 修复 demo |
| v0.5c.1 | OpenAI smoke + SkillCard | 真实模型 smoke 和 SkillCard 候选 |

### 1.1 v0.5b.0 scope

v0.5b.0 只做：

- `SQLiteRepository`
- RepositoryProtocol 收紧
- CLI `new / step / run / status / trace / report`
- dummy worker 端到端运行
- LoopTrace / StopReport 持久化
- stop reason preflight
- DB WAL 和 task lock

不做：

- OpenAI provider
- 真实代码 patch
- 新 ToolResult schema
- 自动 memory promotion
- SkillCard 严格触发

### 1.2 v0.5b.1 scope

v0.5b.1 做：

- `ModelConfig`
- `ModelConfigLoader`
- `OpenAIModelClient`
- `PricingTable`
- `Retry-After` 保留
- 模型错误 evidence
- `ModelAuthError` / `ModelCallError` 到 `WorkerResult` 的完整映射
- CLI `--model-config`

不做：

- Azure OpenAI runtime
- 新 ModelResponse schema
- 把 retry 从 per-loop budget 移到 config

### 1.3 v0.5c.0 scope

v0.5c.0 做：

- `ExecutionWorkerV1`
- `ToolHarness` 最小工具集
- `read_file / write_file / patch_file / list_files / run_shell`
- `examples/demo_pytest_bug`
- dummy deterministic patch demo
- path safety + SandboxRunner 统一复用

不做：

- LearningWorker
- ResearchWorker
- LLMPlanner
- 并发 worker

### 1.4 v0.5c.1 scope

v0.5c.1 做：

- OpenAI smoke demo
- SkillCard candidate
- MemoryCandidate 生成规则固化
- README / examples / troubleshooting

不做：

- 自动 Skill 激活
- 自动 PromotedMemory
- 多模型路由

---

## 2. Compatibility Decisions

本节是本版 PRD 的关键。实现者必须优先遵守这些决策。

### 2.1 BudgetGuard：保留 v0.5a context-based API

**决策：保留。**

不要把 `BudgetGuard` 改成 repo-backed 构造器。不要把接口改成：

```python
BudgetGuard(repo).assert_can_spend(task_id, loop_id, budget, ...)
```

v0.5b/c 必须保留 v0.5a 的调用形态：

```python
BudgetGuard().assert_can_spend(context: ContextPack, *, addl_tokens=0, addl_tool_calls=0, addl_llm_calls=0)
```

原因：

- 已有 `ToolHarness` / `ExecutionWorker` / review-fix 依赖该接口。
- repo-backed 每次记录会让单测和 SQLite 写入更重。
- v0.5b 需要持久化的是 loop trace 和 final usage，不是每个 token 事件。

实现要求：

```python
class BudgetGuard:
    def assert_can_spend(
        self,
        context: ContextPack,
        *,
        addl_tokens: int = 0,
        addl_tool_calls: int = 0,
        addl_llm_calls: int = 0,
    ) -> None:
        ...
```

如果需要持久化预算消耗，用 `UsageAccumulator` 或 `LoopUsageDelta` 在 loop 结束时 flush 到 repository，不改 `BudgetGuard` 基础接口。

---

### 2.2 BudgetAllocation：不删除 v0.5a 字段

**决策：保留现有字段。**

不得删除这些字段：

```python
max_model_retries
retry_base_delay_seconds
retry_max_delay_seconds
allow_shell
allow_file_write
allow_network
```

本版允许新增字段，但必须带默认值，不能破坏旧测试。

建议模型：

```python
class BudgetAllocation(BaseModel):
    phase: LoopPhase
    max_tokens: int = 4000
    max_tool_calls: int = 10
    max_llm_calls: int = 1
    max_wall_clock_seconds: int = 300

    max_model_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0

    allow_shell: bool = True
    allow_file_write: bool = True
    allow_network: bool = False
```

Retry 的来源规则：

```text
per-loop budget 优先。
ModelConfig 只提供默认值。
ContextPack.budget 传入模型调用时覆盖 ModelConfig 默认 retry 配置。
```

---

### 2.3 ContextPack.budget：升级为 BudgetAllocation，但提供兼容层

v0.4.1 中 `ContextPack.budget` 是 `dict[str, object]`。v0.5a 已经有预算模型使用。v0.5b/c 需要统一成：

```python
class ContextPack(BaseModel):
    ...
    budget: BudgetAllocation
```

兼容策略：

```python
@model_validator(mode="before")
def coerce_budget(cls, values):
    budget = values.get("budget")
    if isinstance(budget, dict):
        values["budget"] = BudgetAllocation(**budget)
    return values
```

不得在 worker 中写：

```python
context.budget["max_tokens"]
```

应改为：

```python
context.budget.max_tokens
```

---

### 2.4 ModelResponse：保留 v0.5a schema

**决策：保留。**

不要把：

```python
ModelResponse(content: str, json_data: dict | None)
```

改成：

```python
ModelResponse(content: dict[str, Any], raw_text: str)
```

v0.5b/c 保留：

```python
class ModelResponse(BaseModel):
    content: str
    json_data: dict[str, Any] | None = None
    usage: ModelUsage
    raw_response: dict[str, Any] | None = None
```

ExecutionWorker 读取模型计划时，先用：

```python
response.json_data
```

如果 `json_data is None`，再根据 `content` 做降级解析。

---

### 2.5 API key 解析：保留 client runtime 读取

**决策：client 运行时读取，CLI 做可选 preflight。**

不要让 `ModelConfigLoader` 在加载配置时直接抛 `ModelAuthError`，否则错误不会进入 WorkerRuntime 的映射路径。

配置文件只允许：

```yaml
provider: openai
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY
base_url: https://api.openai.com/v1
```

禁止：

```yaml
api_key: sk-...
```

行为规则：

1. `ModelConfigLoader` 只验证 `api_key_env` 存在且是字符串。
2. `OpenAIModelClient` 在第一次调用时读取 `os.getenv(config.api_key_env)`。
3. 缺 key 时抛 `ModelAuthError`。
4. `WorkerRuntime` 捕获 `ModelAuthError` 并返回：

```python
WorkerResult(
    error="auth_missing_api_key",
    requires_human=True,
    error_type="model_auth",
)
```

5. Orchestrator 看到 `requires_human=True` 时，升级为 `StopReason.HUMAN_REQUIRED`。
6. CLI 可在 run 前做友好 preflight，但不得替代 WorkerRuntime 的错误映射。

---

### 2.6 OpenAI retry：保留 per-call retry 参数

**决策：保留。**

`complete_json` 继续接收 per-call retry 参数：

```python
async def complete_json(
    self,
    messages: list[dict[str, str]],
    *,
    task_id: str,
    max_tokens: int,
    max_retries: int,
    retry_base_delay_seconds: float,
    retry_max_delay_seconds: float,
) -> ModelResponse:
    ...
```

`ModelConfig` 中可提供默认值，但 context budget 优先。

重试耗尽时，不能强行把 `retryable` 改成 `False`。最后一次错误的 retryability 必须保留：

```python
raise ModelCallError(
    message=last_error.message,
    retryable=last_error.retryable,
    status_code=last_error.status_code,
)
```

`Retry-After` 必须保留，并支持：

- 秒数
- RFC1123 HTTP-date

---

### 2.7 ToolResult：v0.5b 不强制加 tool_call_id

**决策：v0.5b 不破坏 ToolResult；v0.5c 可加 optional field。**

v0.5b 中不要强制把 ToolResult 改为必填：

```python
tool_call_id: str
```

v0.5c 如果需要加入，必须是兼容字段：

```python
class ToolResult(BaseModel):
    success: bool
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    tool_call_id: str | None = None
```

`tool_call_id` 生成规则：

```python
tool_call_id = f"TOOL-{task_id}-{loop_id}-{sequence:04d}-{uuid4().hex[:8]}"
```

不得使用：

```python
f"TOOL-{task_id}-{loop_id}-run_shell"
```

因为同一 loop 内多次调用同一工具会重复。

---

### 2.8 allowed_tools：WorkerRuntime 负责，ToolHarness 负责 side-effect

**决策：不把 allowed_tools 迁移进 ToolHarness。**

v0.5b/c 维持职责分离：

```text
WorkerRuntime:
  检查 tool_name 是否在 context.allowed_tools / spec.allowed_tools 中。

ToolHarness:
  检查 side-effect policy、path safety、sandbox、timeout、evidence。
```

ToolHarness 可以接受 `allowed_tools` 参数作为防御性检查，但不是唯一入口。

---

### 2.9 ExecutionWorker schema：保留 actions，新增兼容解析

**决策：保留 v0.5a canonical schema。**

当前 canonical response：

```json
{
  "summary": "...",
  "actions": [
    {"tool_name": "write_file", "args": {...}}
  ]
}
```

不得把 canonical schema 强改成：

```json
{
  "summary": "...",
  "tool_calls": [...],
  "claims": [...]
}
```

v0.5c 可兼容读取 `tool_calls`，但内部先转换成 `actions`：

```python
if "actions" not in plan and "tool_calls" in plan:
    plan["actions"] = plan["tool_calls"]
```

工具失败策略：

```text
默认继续执行后续 actions，累计 evidence。
如果 action.fail_fast == true，首个失败后停止后续 actions。
无论是否 fail_fast，都必须返回已收集 evidence 和 tool_failed 标记。
```

---

### 2.10 Memory non_volatile：保留强判定

**决策：保留。**

不要把现有的：

```python
count_committed_references(candidate_id) >= 2
```

降级成：

```python
source_candidate_state_id == final_best_state_id
```

v0.5c 仍然只生成 MemoryCandidate，不做自动 promotion。Memory promotion 的 schema 迁移留到 v0.6。

---

### 2.11 SkillCard 触发：v0.5c.1 才增强

v0.5a 当前触发：

```text
stop_reason == DONE
best exists
len(best.accepted_check_keys) >= 2
```

v0.5c.1 可加严格条件：

```text
successful_tool_call_count >= 1
regressed_check_keys is empty
```

但必须先把 `LoopTrace.regressed_check_keys` 和 `successful_tool_call_count` 持久化。不得先改 trigger，再让数据缺失。

---

### 2.12 DONE preflight：v0.5b 不改变默认行为

**决策：保留 DONE 可重复查看。**

不要把 `DONE -> reject unless --reset` 作为 v0.5b 行为。

v0.5b 行为：

```text
如果 task 已 DONE：
  hungerloop run <task_id> 输出当前 StopReport，并退出 0。

如果用户显式 --reset：
  v0.5c.1+ 再支持创建新 run。
```

---

### 2.13 StopReport：保留 recommendation 字符串

**决策：保留现有字段，不重命名。**

保留：

```python
recommendation: str
```

允许新增：

```python
recommended_next_actions: list[str] = Field(default_factory=list)
```

不得直接把 `recommendation` 替换成 list。

---

## 3. Domain Schema Changes

### 3.1 StopReason

如果 v0.5a 已经包含以下 enum，则 v0.5b 不再视为新增：

```python
class StopReason(str, Enum):
    DONE = "done"
    HUNGER_EXPIRED = "hunger_expired"
    BLOCKED = "blocked"
    HUMAN_REQUIRED = "human_required"
    HUMAN_PAUSED = "human_paused"
    SAFETY_STOP = "safety_stop"
    ERROR = "error"
```

如果目标分支缺少 `HUMAN_REQUIRED` 或 `ERROR`，v0.5b migration 必须补上。

Worker 到 Orchestrator 的传递规则：

```text
WorkerResult.requires_human == True
  -> Orchestrator StopReason.HUMAN_REQUIRED

WorkerResult.error_type == "model_auth"
  -> Orchestrator StopReason.HUMAN_REQUIRED

WorkerResult.error_type in {"unexpected", "repository", "serialization"}
  -> Orchestrator StopReason.ERROR
```

### 3.2 GoalStatus

保留现有 `GoalStatus` enum，不降级成 `str`。

推荐：

```python
class GoalStatus(str, Enum):
    DONE = "done"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    EXPIRED = "expired"
    ERROR = "error"
```

StopReport 使用：

```python
class StopReport(BaseModel):
    task_id: str
    stop_reason: StopReason
    goal_status: GoalStatus
    recommendation: str
    recommended_next_actions: list[str] = Field(default_factory=list)
```

### 3.3 EvidenceType

所有 evidence type 必须集中定义，禁止散落字符串。

```python
EvidenceType = Literal[
    "shell_output",
    "sandbox_run",
    "tool_call",
    "model_call",
    "model_error",
    "validation_check",
    "human_input",
    "artifact",
    "failure",
]
```

现有字符串映射：

| 现有 | 规范值 |
|---|---|
| shell_run | shell_output |
| sandbox_run | sandbox_run |
| shell_output | shell_output |
| model_error | model_error |
| validation_pass | validation_check |
| tool_result | tool_call |

SQLite 层使用 CHECK constraint：

```sql
CHECK (evidence_type IN (
  'shell_output',
  'sandbox_run',
  'tool_call',
  'model_call',
  'model_error',
  'validation_check',
  'human_input',
  'artifact',
  'failure'
))
```

---

### 3.4 Persistence DTOs

§4.1 RepositoryProtocol references three persistence-only DTOs that v0.5a does not yet define. They are explicit Pydantic models, kept out of `services/` so they don't accidentally couple business logic.

```python
class TaskRecord(BaseModel):
    task_id: str
    raw_goal: str
    status: Literal["pending", "running", "stopped"] = "pending"
    stop_reason: StopReason | None = None
    lock_owner: str | None = None
    locked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EvidenceRecord(BaseModel):
    evidence_id: str
    task_id: str
    loop_id: int | None = None
    evidence_type: EvidenceType
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
```

`EvidenceRecord.payload` carries the type-specific fields (`exit_code`, `model_name`, `tool_name`, `args_summary`, `agent_id`, `provider`, `retryable`, `status_code`, …) that the typed `save_*_as_evidence` helpers in §4.1 fill in. Helpers internally construct an `EvidenceRecord` and call `save_evidence(record)`. The helpers stay in protocol both for v0.5a backward compatibility and to keep call sites readable.

```python
class ArtifactRecord(BaseModel):
    artifact_id: str
    task_id: str
    loop_id: int | None = None
    artifact_type: str
    path: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
```

`ArtifactRecord` is the persistence projection of v0.5a's `Artifact` model. The two may be aliased (`ArtifactRecord = Artifact`) if the field sets line up; they must not diverge.

These three DTOs only live at the repository boundary. Services never import them directly — they go through the typed helper signatures in §4.1.

---

## 4. RepositoryProtocol v0.5b

v0.5b 的 Definition of Done 包括：所有服务中 `repo: Any` 的 TODO 必须收紧到 `RepositoryProtocol`，但不要求一次性删除所有内部 test double。

### 4.1 Required methods

```python
class RepositoryProtocol(Protocol):
    # Task lifecycle
    def create_task(self, task: TaskRecord) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def update_task_status(self, task_id: str, status: str) -> None: ...
    def acquire_task_lock(self, task_id: str, owner: str) -> bool: ...
    def release_task_lock(self, task_id: str, owner: str) -> None: ...

    # Hunger
    def get_hunger_policy(self, task_id: str) -> HungerPolicy: ...
    def save_hunger_policy(self, task_id: str, policy: HungerPolicy) -> None: ...
    def get_hunger_clock(self, task_id: str) -> HungerClockState: ...
    def save_hunger_clock(self, task_id: str, clock: HungerClockState) -> None: ...
    def get_hunger_ledger(self, task_id: str) -> HungerLedger: ...
    def save_hunger_ledger(self, ledger: HungerLedger) -> None: ...
    def get_hunger_item(self, item_id: str) -> HungerItem | None: ...
    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]: ...
    def save_hunger_item(self, item: HungerItem) -> None: ...
    def get_open_hunger_items(self, task_id: str) -> list[HungerItem]: ...
    def select_highest_priority_open_hunger_item(self, task_id: str) -> HungerItem | None: ...

    # Phase / loop
    def next_loop_id(self, task_id: str) -> int: ...
    def get_last_phase(self, task_id: str) -> LoopPhase | None: ...
    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None: ...
    def save_loop_plan(self, plan: LoopPlan) -> None: ...
    def get_loop_plan(self, task_id: str, loop_id: int) -> LoopPlan | None: ...
    def save_loop_trace(self, trace: LoopTrace) -> None: ...
    def get_loop_trace(self, task_id: str, loop_id: int) -> LoopTrace | None: ...
    def list_loop_traces(self, task_id: str) -> list[LoopTrace]: ...

    # Agent / workers
    def get_agent_spec(self, agent_id: str) -> AgentSpec: ...
    def save_agent_spec(self, spec: AgentSpec) -> None: ...
    def save_worker_result(self, result: WorkerResult) -> None: ...
    def list_worker_results(self, task_id: str, loop_id: int | None = None) -> list[WorkerResult]: ...

    # Candidate / best state
    def save_candidate(self, candidate: CandidateState) -> None: ...
    def get_candidate(self, candidate_id: str) -> CandidateState | None: ...
    def mark_candidate_committed(self, candidate_id: str) -> None: ...
    def mark_candidate_rejected(self, candidate_id: str) -> None: ...
    def get_best_state(self, task_id: str) -> BestState | None: ...
    def save_best_state(self, best: BestState) -> None: ...

    # Validation / checks
    def save_validation_report(self, report: ValidationReport) -> None: ...
    def get_validation_report(self, validation_id: str) -> ValidationReport | None: ...
    def get_items_for_check_keys(self, task_id: str, check_keys: list[str]) -> list[HungerItem]: ...
    def save_accepted_checks(self, task_id: str, validation: ValidationReport) -> None: ...
    def list_accepted_checks(self, task_id: str) -> list[str]: ...

    # Evidence / artifacts
    def save_evidence(self, evidence: EvidenceRecord) -> str: ...
    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None: ...
    def count_evidence_by_type(
        self,
        task_id: str,
        evidence_ids: list[str],
        evidence_type: EvidenceType | Literal["any"],
    ) -> int: ...
    def save_shell_output_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        label: str,
        argv: list[str],
        cwd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> str: ...
    def save_model_error_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        provider: str,
        model_name: str,
        error_type: str,
        message: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> str: ...
    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[ArtifactRecord]: ...
    def save_artifact(self, artifact: ArtifactRecord) -> str: ...

    # Stagnation / failures
    def add_failure_from_validation(self, report: ValidationReport) -> None: ...
    def get_no_progress_streak(self, task_id: str) -> int: ...
    def reset_no_progress_streak(self, task_id: str) -> None: ...
    def increment_no_progress_streak(self, task_id: str) -> int: ...

    # Memory / skills
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None: ...
    def list_memory_candidates(self, task_id: str | None = None) -> list[MemoryCandidate]: ...
    def count_committed_references(self, candidate_id: str) -> int: ...
    def save_skill_card(self, card: SkillCard) -> None: ...
    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]: ...

    # Usage / events
    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot: ...
    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> None: ...
    def append_event(self, event_type: str, payload: dict[str, object]) -> None: ...

    # StopReport
    def save_stop_report(self, report: StopReport) -> None: ...
    def get_stop_report(self, task_id: str) -> StopReport | None: ...
```

`StopReport` is persisted in its own table (§5.2 `stop_reports`), keyed by `task_id` (one row per task — re-runs after `--reset` overwrite). `tasks.stop_reason` is the indexable copy used by preflight; `stop_reports.payload_json` is the full report consumed by `hungerloop status` / `hungerloop report`. Implementations must keep these two columns consistent within a single transaction.

### 4.2 Compatibility rule

`InMemoryRepository` must implement the same protocol as `SQLiteRepository`.

All new repository tests must run against both implementations:

```python
@pytest.mark.parametrize("repo_factory", [InMemoryRepository, SQLiteRepository])
def test_protocol_behavior(repo_factory):
    ...
```

---

## 5. SQLiteRepository v0.5b.0

### 5.1 SQLite operating mode

SQLite must use WAL:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

Multi-process rule:

```text
Multiple readers allowed.
Only one runner may own a task_id lock at a time.
hungerloop status/report/trace are read-only and may run while task is locked.
hungerloop run must acquire task lock before orchestrator starts.
```

Task lock uses `tasks.lock_owner` and `tasks.locked_at`.

### 5.2 Core tables

```sql
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  raw_goal TEXT NOT NULL,
  status TEXT NOT NULL,
  stop_reason TEXT,
  lock_owner TEXT,
  locked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE hunger_policies (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE hunger_clock_states (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE hunger_items (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  priority REAL NOT NULL,
  gap_score REAL NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE best_states (
  task_id TEXT PRIMARY KEY,
  state_id TEXT NOT NULL,
  validation_id TEXT,
  updated_at_loop INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE candidate_states (
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
```

### 5.3 Operational tables

```sql
CREATE TABLE loop_plans (
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, loop_id)
);

CREATE TABLE loop_traces (
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  stop_reason TEXT,
  committed INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, loop_id)
);

CREATE TABLE worker_results (
  result_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  error_type TEXT,
  requires_human INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL
);

CREATE TABLE agent_specs (
  agent_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER,
  evidence_type TEXT NOT NULL CHECK (evidence_type IN (
    'shell_output',
    'sandbox_run',
    'tool_call',
    'model_call',
    'model_error',
    'validation_check',
    'human_input',
    'artifact',
    'failure'
  )),
  payload_json TEXT NOT NULL
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER,
  artifact_type TEXT NOT NULL,
  path TEXT,
  payload_json TEXT NOT NULL
);

CREATE TABLE failures (
  failure_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER,
  payload_json TEXT NOT NULL
);

CREATE TABLE usage_snapshots (
  task_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);

CREATE TABLE no_progress_streaks (
  task_id TEXT PRIMARY KEY,
  streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  task_id TEXT,
  loop_id INTEGER,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
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
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE stop_reports (
  task_id TEXT PRIMARY KEY,
  stop_reason TEXT NOT NULL,
  goal_status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

`stop_reports` is one row per task. Re-runs after `--reset` (v0.5c.1+) overwrite the row in the same transaction that flips `tasks.stop_reason` back to `NULL`. CLI status / report read this table via `get_stop_report(task_id)`.

### 5.4 Required indexes

```sql
CREATE INDEX idx_hunger_items_task_status ON hunger_items(task_id, status);
CREATE INDEX idx_evidence_task_loop ON evidence(task_id, loop_id);
CREATE INDEX idx_artifacts_task_loop ON artifacts(task_id, loop_id);
CREATE INDEX idx_worker_results_task_loop ON worker_results(task_id, loop_id);
CREATE INDEX idx_loop_traces_task ON loop_traces(task_id);
CREATE INDEX idx_events_task_loop ON events(task_id, loop_id);
CREATE INDEX idx_validation_reports_task_loop ON validation_reports(task_id, loop_id);
CREATE INDEX idx_candidate_states_task_loop ON candidate_states(task_id, loop_id);
```

---

## 6. ModelConfig v0.5b.1

### 6.1 Schema

```python
class ModelProvider(str, Enum):
    DUMMY = "dummy"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"

class ModelConfig(BaseModel):
    provider: ModelProvider
    model_name: str
    api_key_env: str | None = None
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.1
    max_output_tokens: int = 2048
    timeout_seconds: int = 60

    # Defaults only. ContextPack.budget overrides these.
    max_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0

    fail_on_unknown_model_price: bool = False
```

### 6.2 Loader behavior

```python
class ModelConfigLoader:
    def load(self, path: Path) -> ModelConfig:
        config = ModelConfig(**yaml.safe_load(path.read_text()))

        if config.provider == ModelProvider.AZURE_OPENAI:
            raise NotImplementedError(
                "Azure OpenAI runtime is not shipped in v0.5b/c. Use provider=dummy or provider=openai."
            )

        if config.provider == ModelProvider.OPENAI and not config.api_key_env:
            raise ValueError("provider=openai requires api_key_env")

        return config
```

The loader does not read the API key value. It validates only the environment variable name.

### 6.3 Example config

```yaml
provider: openai
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY
base_url: https://api.openai.com/v1
temperature: 0.1
max_output_tokens: 2048
timeout_seconds: 60
max_retries: 2
retry_base_delay_seconds: 1.0
retry_max_delay_seconds: 30.0
fail_on_unknown_model_price: false
```

---

## 7. PricingTable

### 7.1 Default behavior

v0.5b default must not fail on unknown model price.

```text
Unknown model price:
  append_event("unknown_model_pricing", ...)
  return cost_usd = 0.0
  continue
```

`fail_on_unknown_model_price=True` is allowed but not default.

### 7.2 Schema

```python
class PricingTable:
    def __init__(self, fail_on_unknown: bool = False):
        self.fail_on_unknown = fail_on_unknown
        self.prices = {
            "gpt-4o-mini": Price(input_per_million=0.15, output_per_million=0.60),
            "gpt-4o": Price(input_per_million=5.00, output_per_million=15.00),
            "gpt-4o-2024-08-06": Price(input_per_million=2.50, output_per_million=10.00),
        }

    def estimate(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        price = self.prices.get(model_name)
        if price is None:
            if self.fail_on_unknown:
                raise UnknownModelPricingError(model_name)
            return 0.0
        return (
            input_tokens / 1_000_000 * price.input_per_million
            + output_tokens / 1_000_000 * price.output_per_million
        )
```

The repository event is written by the caller because `PricingTable` has no repository dependency.

---

## 8. OpenAIModelClient v0.5b.1

### 8.1 Preserve constructor style

The constructor keeps v0.5a's dependencies. `cost_guard` and `repo` are required, not optional — they own task-ceiling accounting and evidence writing (see §8.7).

```python
class OpenAIModelClient(ModelClient):
    def __init__(
        self,
        config: ModelConfig,
        cost_guard: CostGuard,
        pricing_table: PricingTable,
        repo: RepositoryProtocol,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.config = config
        self.cost_guard = cost_guard
        self.pricing_table = pricing_table
        self.repo = repo
        self.http_client_factory = http_client_factory or self._default_client
```

Do not require `api_key: str` in constructor — the env var is read on first call (see §2.5).

### 8.2 Full call signature

```python
async def complete_json(
    self,
    messages: list[dict[str, str]],
    *,
    task_id: str,
    loop_id: int,
    agent_id: str,
    max_tokens: int,
    max_retries: int,
    retry_base_delay_seconds: float,
    retry_max_delay_seconds: float,
) -> ModelResponse:
    ...
```

`task_id`, `loop_id`, and `agent_id` are required for evidence and error accounting. `agent_id` must be threaded into both `model_call` and `model_error` evidence rows so trace queries can attribute calls to a specific worker.

### 8.3 `_call_once` signature

```python
async def _call_once(
    self,
    *,
    api_key: str,
    messages: list[dict[str, str]],
    task_id: str,
    loop_id: int,
    agent_id: str,
    max_tokens: int,
) -> ModelResponse:
    ...
```

### 8.4 Async httpx usage

Must use `httpx.AsyncClient`:

```python
async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
    response = await client.post(
        f"{self.config.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": self.config.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.config.temperature,
        },
    )
```

Do not use `await httpx.post(...)`.

### 8.5 Error mapping

```text
Missing API key -> ModelAuthError(requires_human=True)
401/403 -> ModelAuthError(requires_human=True)
429 -> ModelCallError(retryable=True)
5xx -> ModelCallError(retryable=True)
Other 4xx -> ModelCallError(retryable=False)
TransportError -> ModelCallError(retryable=True)
Invalid JSON -> ModelCallError(retryable=False)
Missing content -> ModelCallError(retryable=False)
```

Retry exhausted must preserve `last_error.retryable`.

### 8.6 Retry-After

`Retry-After` parser must support:

- integer seconds
- RFC1123 date

If invalid, fallback to exponential delay with jitter.

### 8.7 Side-effects ownership

OpenAIModelClient retains v0.5a's responsibility for two side-effects. They do not move to WorkerRuntime, ExecutionWorker, or any other layer.

**Cost accounting (per successful call):**

```python
usage = ModelUsage(
    input_tokens=...,
    output_tokens=...,
    cost_usd=self.pricing_table.estimate(self.config.model_name, input_tokens, output_tokens),
)
self.cost_guard.record_llm_usage(task_id, usage)
```

This is what advances the task-level ceiling (`HungerPolicy.max_total_cost_usd`, `max_total_tokens`). Skipping it lets a runaway worker silently blow the ceiling.

**Evidence writes (per call, success or failure):**

```text
Success path:
  self.repo.save_model_call_as_evidence(
      task_id=task_id, loop_id=loop_id, agent_id=agent_id,
      provider=self.config.provider.value, model=self.config.model_name,
      input_tokens=..., output_tokens=..., cost_usd=...,
      response_preview=...,
  )

Retry-exhausted / non-retryable path:
  self.repo.save_model_error_as_evidence(
      task_id=task_id, loop_id=loop_id, agent_id=agent_id,
      provider=..., model=..., error_type=type(exc).__name__,
      error_message=str(exc), retryable=last_error.retryable,
      status_code=last_error.status_code,
  )
```

Both rows must carry the threaded `loop_id` and `agent_id`; never use `loop_id=0` or `agent_id=None` sentinels.

**WorkerRuntime contract:** WorkerRuntime sees only the `ModelResponse` (success) or the bubbled-up exception (failure). It does not write `model_call` evidence and does not touch `cost_guard`. It maps the exception to `WorkerResult` per §9.2.

**Pricing fallback:** When `PricingTable.estimate` returns `0.0` for an unknown model, the client still calls `cost_guard.record_llm_usage` (with `cost_usd=0.0`) and the caller (e.g. `WorkerRuntime` or the loop wiring) is responsible for emitting a single `unknown_model_pricing` event per task — this keeps `PricingTable` itself repository-free as stated in §7.

---

## 9. WorkerRuntime

### 9.1 Responsibilities

WorkerRuntime is the thick shell around workers.

It must:

1. Resolve `AgentSpec` to the concrete worker.
2. Check `context.allowed_tools` and `spec.allowed_tools` before tool dispatch.
3. Call `BudgetGuard.assert_can_spend(context, ...)` before LLM/tool operations.
4. Wrap worker execution in `asyncio.wait_for` using `context.budget.max_wall_clock_seconds`.
5. Catch known model/tool/budget errors and return `WorkerResult`.
6. Never allow recoverable worker errors to escape the orchestrator.

### 9.2 Error mapping

```python
except ModelAuthError as exc:
    return WorkerResult(
        error=str(exc),
        error_type="model_auth",
        requires_human=True,
    )

except ModelCallError as exc:
    return WorkerResult(
        error=str(exc),
        error_type="model_retryable" if exc.retryable else "model_non_retryable",
        requires_human=not exc.retryable,
    )

except WorkerBudgetExceeded as exc:
    return WorkerResult(error=str(exc), error_type="budget", requires_human=False)

except asyncio.TimeoutError:
    return WorkerResult(error="worker_timeout", error_type="timeout", requires_human=False)
```

Unexpected errors become `error_type="unexpected"`, and Orchestrator maps them to `StopReason.ERROR` after writing trace.

---

## 10. ToolHarness v0.5c

### 10.1 Responsibilities

ToolHarness is responsible for:

- tool registry
- side-effect policy
- path safety
- SandboxRunner integration
- timeout
- evidence for both success and failure

It is not the main owner of `allowed_tools`; WorkerRuntime owns that.

### 10.2 Tool registry

```python
TOOL_REGISTRY = {
    "read_file": ReadFileTool(),
    "write_file": WriteFileTool(),
    "patch_file": PatchFileTool(),
    "list_files": ListFilesTool(),
    "run_shell": RunShellTool(),
}
```

### 10.3 Path rules

All file paths must pass:

```python
resolve_workspace_path(candidate_workspace_root, user_path)
```

Forbidden:

- absolute path
- `..` escape
- symlink escape
- writing to `best/`

### 10.4 Shell rules

`run_shell` must use `SandboxRunner.run_argv`.

Allowed args:

```json
{"argv": ["pytest", "tests/"], "timeout": 60}
```

Forbidden args:

```json
{"cmd": "pytest tests/"}
```

### 10.5 Failed tool behavior

If a tool raises a path, validation, subprocess, or runtime error, ToolHarness returns failed `ToolResult` and records evidence.

It should not propagate ordinary tool errors to Orchestrator.

```python
ToolResult(
    success=False,
    error_type="invalid_args" | "runtime" | "timeout" | "permission",
    error="...",
    evidence_ids=[...],
)
```

---

## 11. RuleBasedPlanner

### 11.1 v0.5b planner

v0.5b uses only RuleBasedPlanner.

Selection rule:

```text
active_items = hunger items where status in OPEN/WORKING and gap_score > 0
score = priority * gap_score
pick highest score item
route to execution_worker_v1
```

If no active item exists:

```text
return empty plan
```

### 11.2 Empty plan behavior

Empty plan does not immediately mark task BLOCKED.

Orchestrator behavior:

```text
empty plan:
  reject candidate workspace if created
  increment global no-progress streak
  if streak >= threshold -> BLOCKED
  else return LoopTrace(committed=False, next_action="continue")
```

Only after stagnation threshold does StopReason become `BLOCKED`.

---

## 12. Orchestrator loop semantics

### 12.1 Loop ID and loop_count

`repo.next_loop_id(task_id)` is the source of `loop_id`.

`clock.loop_count` semantics:

```text
clock.loop_count = completed loop attempts.
```

Increment rule:

```text
If HungerEngine.tick returns stop at loop start:
  do not increment loop_count.

If candidate workspace is created and a loop attempt begins:
  increment loop_count exactly once in finally.

This applies even if worker/model/tool validation fails.
```

Required test:

```text
tick_stop_does_not_increment_loop_count
worker_error_does_increment_loop_count_once
validation_fail_does_increment_loop_count_once
```

### 12.2 StopReason handling

Orchestrator must handle all StopReason values:

```text
DONE
HUNGER_EXPIRED
BLOCKED
HUMAN_REQUIRED
HUMAN_PAUSED
SAFETY_STOP
ERROR
```

`SafetyStopError` can occur mid-loop. It must be caught immediately and converted into `StopReport(stop_reason=SAFETY_STOP)`.

Do not wait until the next tick.

### 12.3 Worker result aggregation

Workers do not write to CandidateState directly.

Correct statement:

```text
Workers write artifacts and evidence to candidate workspace and return WorkerResult.
Integrator creates CandidateState from WorkerResult list.
```

Forbidden design:

```text
Worker receives mutable CandidateState and mutates it directly.
```

---

## 13. ExecutionWorkerV1

### 13.1 Canonical input/output

Worker receives:

```python
AgentSpec
ContextPack
workspace_root: Path
```

Canonical model response remains:

```json
{
  "summary": "...",
  "actions": [
    {"tool_name": "read_file", "args": {"path": "..."}},
    {"tool_name": "write_file", "args": {"path": "...", "content": "..."}},
    {"tool_name": "run_shell", "args": {"argv": ["pytest", "tests/"], "timeout": 60}}
  ],
  "proposed_next_actions": []
}
```

Compatible alias:

```json
{"tool_calls": [...]}
```

Internally converted to `actions`.

### 13.2 Prompt template

Add `services/prompts.py`.

```python
EXECUTION_WORKER_PROMPT = """
You are execution_worker_v1.

Mission:
{mission}

Candidate workspace:
{candidate_workspace_ref}

Allowed tools:
{allowed_tools}

Acceptance checks:
{acceptance_checks}

Rules:
- You may only operate inside the candidate workspace.
- Use argv arrays for shell commands.
- Do not use absolute paths.
- Return strict JSON with keys: summary, actions, proposed_next_actions.
- Do not claim completion. ValidationGate decides completion.
"""
```

All workers must use central prompt templates, not ad-hoc strings.

### 13.3 Failure strategy

Default:

```text
Run all actions, collect evidence.
```

Optional:

```json
{"tool_name": "run_shell", "args": {...}, "fail_fast": true}
```

If `fail_fast` and tool fails:

```text
stop remaining actions
return WorkerResult with collected evidence, tool_failed=True
```

---

## 14. ModelConfig wiring in CLI

### 14.1 CliContext

`CliContext` must carry:

```python
class CliContext(BaseModel):
    repo: RepositoryProtocol
    workspace_manager: WorkspaceManager
    model_client: ModelClient
    model_config: ModelConfig
    orchestrator_factory: OrchestratorFactory
```

### 14.2 CLI flags

`new` does not require model config.

```bash
hungerloop new --goal-file goal.md --accept-file accept.yaml
```

`step` and `run` may require model config depending on worker type.

```bash
hungerloop run <task_id> --model-config model.openai.yaml
hungerloop step <task_id> --model-config model.dummy.yaml
```

### 14.3 Provider behavior

```text
provider=dummy:
  no API key required

provider=openai:
  api_key_env required
  actual key read by OpenAIModelClient on call

provider=azure_openai:
  loader rejects in v0.5b/c
```

---

## 15. Acceptance check input

### 15.1 No string DSL in v0.5b

Do not implement this as primary interface:

```bash
--accept 'shell_exit_zero:argv=["pytest","tests/"]:timeout=60'
```

It is too fragile because `:` and quotes collide.

### 15.2 Use accept-file

Primary interface:

```bash
hungerloop new \
  --goal "Fix failing pytest test" \
  --accept-file accept.yaml
```

`accept.yaml`:

```yaml
core_acceptance_mode: all
core_acceptance_checks:
  - check_type: shell_exit_zero
    params:
      argv: ["pytest", "tests/"]
      timeout: 60
    description: "pytest passes"
  - check_type: file_exists
    params:
      path: "src/foo.py"
    description: "source file exists"
```

`--accept` string DSL can be added later as convenience, but not in v0.5b.0.

### 15.3 H-002 evidence item

`RequirementCompiler` automatically creates H-002:

```text
H-002: Sufficient evidence
check: evidence_count_min(any, 1)
```

This is not supplied by the user.

`accept.yaml` only configures H-001.

### 15.4 acceptance_mode

If `HungerItem.acceptance_mode` already exists, preserve it.

If absent on target branch, add with default:

```python
acceptance_mode: Literal["all", "any"] = "all"
```

---

## 16. CLI preflight

CLI must check previous stop reason before calling Orchestrator.

### 16.1 Preflight rules

```text
DONE:
  print existing StopReport; exit 0

HUNGER_EXPIRED:
  require --refill or hunger refill first

HUMAN_REQUIRED:
  require --resume after user has fixed requirement

HUMAN_PAUSED:
  require --resume or hunger resume

BLOCKED:
  require hunger unblock <item_id> or --unblock-all

SAFETY_STOP:
  require --raise-cost-ceiling or policy edit

ERROR:
  require --resume-error or --reset
```

### 16.2 --resume behavior

`--resume` must do real state mutation only for resumable states.

For `HUMAN_PAUSED`:

```python
clock.frozen = False
repo.save_hunger_clock(task_id, clock)
repo.append_event("hunger_resumed", ...)
```

For `HUMAN_REQUIRED`:

```text
--resume means user asserts the missing condition is resolved.
CLI records event human_requirement_resolved.
```

### 16.3 repair-state command

Add the missing command:

```bash
hungerloop repair-state <task_id> --check
hungerloop repair-state <task_id> --fix
```

Purpose:

```text
Detect and repair divergence between SQLite state and workspace state.
```

v0.5b.0 may implement only `--check`.

---

## 17. Budget hierarchy

Three budget levels exist.

```text
Task ceiling:
  HungerPolicy.max_total_cost_usd
  HungerPolicy.max_total_tokens

Loop budget:
  BudgetAllocation for one loop attempt

Worker/action budget:
  ContextPack.budget consumed by WorkerRuntime and ToolHarness
```

Rules:

1. CostGuard enforces task ceiling.
2. BudgetGuard enforces ContextPack budget.
3. BudgetGuard keeps in-memory per-loop deltas.
4. Orchestrator flushes loop usage to repository after each loop.
5. Phase change creates a new LoopBudget; it does not reset task ceiling.

---

## 18. LoopTrace and StopReport

### 18.1 LoopTrace additive fields

Do not remove existing fields. Add optional fields with defaults.

```python
class LoopTrace(BaseModel):
    ...
    tokens_consumed_this_loop: int = 0
    cost_this_loop_usd: float = 0.0
    llm_calls_this_loop: int = 0
    tool_calls_this_loop: int = 0
    worker_timeout: bool = False
    blocked_items_added: list[str] = Field(default_factory=list)
    model_error_ids: list[str] = Field(default_factory=list)
    tool_error_ids: list[str] = Field(default_factory=list)
    newly_passed_check_keys: list[str] = Field(default_factory=list)
    regressed_check_keys: list[str] = Field(default_factory=list)
    candidate_workspace_ref: str | None = None
    best_state_id_after_loop: str | None = None
    successful_tool_call_count: int = 0
```

### 18.2 StopReport additive fields

Preserve:

```python
recommendation: str
```

Add:

```python
class StopReport(BaseModel):
    ...
    final_best_state_id: str | None = None
    accepted_check_keys_count: int = 0
    total_loops: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    remaining_hunger_items: list[str] = Field(default_factory=list)
    blocked_hunger_items: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)
    resume_hint: str | None = None
```

---

## 19. MemoryCandidate and SkillCard

### 19.1 v0.5c MemoryCandidate

v0.5c only proposes candidates. It does not promote automatically.

Promotion predicates are documented but not auto-run.

```text
action_verified:
  at least one evidence_id comes from accepted check evidence

reusable:
  candidate content does not reference task-specific paths or ids

non_volatile:
  existing strong predicate remains count_committed_references(candidate_id) >= 2

traceable:
  all candidate.evidence_ids are subset of best.evidence_ids
```

### 19.2 SkillCard trigger

v0.5c.1 trigger:

```text
stop_reason == DONE
best_state exists
len(best.accepted_check_keys) >= 2
successful_tool_call_count >= 1
last_trace.regressed_check_keys is empty
```

If `successful_tool_call_count` or `regressed_check_keys` are unavailable, do not enable strict trigger yet.

---

## 20. Demo requirements

### 20.1 demo_pytest_bug

Directory:

```text
examples/demo_pytest_bug/
  src/
  tests/
  accept.yaml
  hunger_policy.yaml
  model.dummy.yaml
  model.openai.yaml
```

`accept.yaml`:

```yaml
core_acceptance_mode: all
core_acceptance_checks:
  - check_type: shell_exit_zero
    params:
      argv: ["pytest", "tests/"]
      timeout: 60
    description: "pytest passes"
```

Success path:

```bash
hungerloop new --goal "Fix the failing pytest test" --accept-file examples/demo_pytest_bug/accept.yaml
hungerloop run <task_id> --model-config examples/demo_pytest_bug/model.dummy.yaml
```

Expected:

```text
StopReason.DONE
Best workspace contains patched code
accepted_check_keys_count >= 2
at least one tool evidence
```

### 20.2 demo_hunger_expired

Same demo, but policy sets very low loop count.

Expected:

```text
StopReason.HUNGER_EXPIRED
remaining_hunger_items non-empty
recommended_next_actions non-empty
```

### 20.3 demo_blocked

Use an impossible acceptance check.

Expected:

```text
StopReason.BLOCKED after stagnation threshold
blocked_hunger_items non-empty
```

---

## 21. v0.5a to v0.5b migration checklist

### 21.1 Files likely touched

```text
models/context.py
models/planning.py
models/tracing.py
models/worker.py
models/enums.py
repository/protocol.py
repository/in_memory_repo.py
repository/sqlite_repo.py
repository/schema.py
services/model_config.py
services/openai_model_client.py
services/pricing_table.py
services/worker_runtime.py
services/tool_harness.py
services/execution_worker.py
services/orchestrator.py
services/prompts.py
cli/main.py
cli/new_cmd.py
cli/run_cmd.py
cli/status_cmd.py
cli/trace_cmd.py
cli/report_cmd.py
```

### 21.2 Tests expected to change

```text
test_model_config_loader.py
test_openai_model_client.py
test_worker_runtime.py
test_cli_run.py
test_preflight.py
test_repository_sqlite.py
test_execution_worker.py
test_tool_harness.py
```

### 21.3 Tests not expected to break

```text
test_budget_guard.py
test_commit_manager.py
test_validation_gate.py
test_workspace_isolation.py
test_path_safety.py
test_sandbox_runner.py
test_stagnation_detector.py
```

If these break, the implementation likely violated compatibility constraints.

---

## 22. Acceptance criteria

### v0.5b.0

```text
1. SQLiteRepository passes same protocol tests as InMemoryRepository.
2. CLI new creates task in SQLite.
3. CLI run can drive a dummy worker task to HUNGER_EXPIRED (and to DONE
   only when acceptance is a tautology check such as `always_pass`).
   Real-tool DONE paths require ToolHarness and are deferred to v0.5c.0.
4. CLI status reads StopReport from `stop_reports` table after process restart.
5. CLI trace/report read SQLite state.
6. WAL mode enabled; busy_timeout >= 5000 ms.
7. Same task_id cannot be run by two runners concurrently
   (tasks.lock_owner gate).
8. Existing v0.5a unit tests remain green unless explicitly updated
   per §21.2.
```

### v0.5b.1

```text
1. ModelConfig loader supports dummy/openai.
2. Azure config is rejected clearly.
3. OpenAIModelClient uses httpx.AsyncClient.
4. Missing OPENAI_API_KEY maps to HUMAN_REQUIRED.
5. 429/5xx retry and Retry-After are preserved.
6. retry exhausted preserves retryable flag.
7. Unknown pricing defaults to 0.0 and appends event.
8. Model errors are saved as evidence.
```

### v0.5c.0

```text
1. ExecutionWorkerV1 uses summary/actions schema.
2. ToolHarness supports read/write/patch/list/run_shell.
3. run_shell uses SandboxRunner argv only.
4. All tool paths are candidate-workspace confined.
5. Tool failure returns failed ToolResult with evidence.
6. demo_pytest_bug dummy path reaches DONE.
7. HUNGER_EXPIRED demo works.
8. BLOCKED demo works.
```

### v0.5c.1

```text
1. OpenAI smoke demo completes at least one validated loop.
2. SkillCard candidate generated only when strict trigger data exists.
3. MemoryCandidate generation does not promote automatically.
4. README documents model config, accept-file and stop preflight.
```

---

## 23. Non-goals

v0.5b/c will not implement:

- LLMPlanner
- LearningWorker
- ResearchWorker
- multi-worker parallelism
- Azure OpenAI runtime
- automatic memory promotion
- full skill registry activation
- web UI
- FastAPI
- browser automation
- vector database memory

---

## 24. Summary

The most important change in this rewrite is not new functionality. It is compatibility discipline.

v0.5b/c must move HungerLoop forward in this order:

```text
1. Persist state.
2. Make CLI recoverable.
3. Add model config without breaking ModelResponse.
4. Add OpenAI runtime without moving retry ownership.
5. Add real ExecutionWorker without rewriting actions schema.
6. Add tools without escaping candidate workspace.
7. Add demo coverage.
```

Do not rewrite v0.5a while implementing v0.5b/c.

If an implementation requires broad test rewrites across BudgetGuard, ModelResponse, ExecutionWorker schema, or OpenAI retry behavior, it is probably violating this PRD.
