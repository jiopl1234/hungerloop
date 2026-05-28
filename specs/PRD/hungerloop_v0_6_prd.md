# HungerLoop v0.6 PRD — Mission Runtime Evolution

**版本**: v0.6.0
**日期**: 2026-05-17
**基线**: v0.5f (cross-loop context) + v0.5e (memory/skill lifecycle)
**变更性质**: 演进式扩展，保留 hunger-driven 核心设计
**目标**: 将 HungerLoop 从单 worker 循环执行引擎演进为支持多 worker 协作、结构化交接、分层验证的 mission runtime，同时保持饥饿度驱动的资源约束模型。

---

## 0. 版本定位与设计原则

### 0.1 v0.6 不是什么

v0.6 **不是**重写，**不是**放弃 hunger-driven 模型，**不是**变成 Droid missions 的克隆。

v0.6 是在 HungerLoop 已有的坚实基础上，针对 `report1.md` 识别的 mission-runtime 短板，做**兼容性演进**：

```text
保留：
- HungerEngine 的 drive_budget / work_pressure / phase 模型
- Check-level commit (I-3)
- Workspace isolation (I-4)
- Targeted validation (I-5)
- Cost guard (I-8)
- BLOCKED ≠ DONE (I-9)
- Rule-based requirement compilation (I-10)
- SQLite persistence + trace/report observability

演进：
- 单 worker → 多 worker 调度（保留 hunger 选择逻辑）
- WorkerResult → WorkerHandoff（结构化交接）
- 单阶段 ValidationGate → 多阶段验证流水线
- 隐式上下文 → 显式 mission artifacts
- CLI task 操作 → mission cockpit
```

### 0.2 核心设计原则

1. **Hunger-first**: 所有 worker 调度、验证触发、资源分配都由 `HungerEngine.tick()` 驱动，不引入独立的 mission scheduler。
2. **Backward compatible**: v0.5f 的单 worker 任务必须在 v0.6 中零修改运行。
3. **Incremental adoption**: 新能力（多 worker、handoff、validator）通过 opt-in 启用，不强制所有任务升级。
4. **No LLM planning**: v0.6 仍使用 `RuleBasedPlanner`，只是从"选 1 个 item 给 1 个 worker"扩展为"选 N 个 item 分配给 M 个 worker"。LLMPlanner 是 v0.7。
5. **Deterministic validation**: 新增的 scrutiny/user-testing validator 仍基于确定性规则，不引入 LLM-as-judge。

---

## 1. 问题陈述

### 1.1 当前 HungerLoop 的优势

- **Check-driven convergence**: 每轮必须让至少一个 check 从 fail 变 pass 才能 commit，避免空转。
- **Resource-bounded execution**: drive_budget 衰减 + cost ceiling 双重约束，任务不会无限消耗资源。
- **Workspace safety**: candidate/best 隔离 + path safety + sandbox，rejected candidate 不污染已提交状态。
- **Observability**: SQLite + events + traces + reports，完整审计链。
- **Cross-loop context**: v0.5f 已实现 prior-loop summary + evidence + failures 注入。

### 1.2 当前短板（来自 report1.md）

**P0: 缺少真正的 mission / 多 agent 调度模型**

- `RuleBasedPlanner` 当前 `n = min(1, budget.max_workers_per_loop)`，实际只生成 1 个 assignment。
- 没有 worker 依赖 DAG、fan-out/join、并发调度、取消、重试。
- 没有 worker role / skill 类型分化。

**P0: worker handoff 太弱**

- `WorkerResult` 只有 `summary / artifact_ids / evidence_ids / error`，不足以表达：
  - blocker
  - follow-up
  - partial completion
  - 需要 orchestrator 决策的上下文

**P0: 缺少 mission artifact contract**

- 没有等价于 `mission.md / features.yaml / validation-contract.yaml / services.yaml / AGENTS.md` 的长期契约文件。
- 当前 `HungerLedger` 在内存/SQLite 中，但没有人类可读的 mission spec。

**P0: 验证体系刚性强，但 mission 泛化不足**

- `AcceptanceCheckType` 只有 6 种：`file_exists / shell_exit_zero / evidence_count_min / artifact_type_exists / human_approval / llm_judge`。
- 缺少 validation-contract 式行为断言、自动注入 validator worker、reviewer/scrutiny、user-testing。

**P1: pause/resume 粒度偏 task 级**

- 已有 `HUMAN_PAUSED / BLOCKED / ERROR / resume preflight`，但缺少：
  - 暂停单个 worker
  - retry 某个 assignment
  - restart feature
  - preempt 当前 worker

**P1: 缺少 services.yaml 类运行环境契约**

- 有 model config、sandbox、cost guard，但没有统一声明：
  - 服务启动/停止/healthcheck
  - 端口边界
  - CLI/tool manifest

**P1: CLI 不是 mission-native**

- 当前是 `task/run/status/report/trace` 风格，不是 mission cockpit。

---

## 2. v0.6 目标

v0.6 的目标是让 HungerLoop 从"单 worker 循环修复引擎"演进为"hunger-driven mission runtime"：

```text
v0.5f: task → loops → single worker → candidate → validation → commit
v0.6:  mission → phases → multi-worker DAG → handoffs → multi-stage validation → commit
```

同时保持：

```text
- HungerEngine 仍是唯一的 tick 入口
- drive_budget 仍控制循环预算
- check-level commit 仍是提交条件
- workspace isolation 仍是安全边界
```

---

## 3. 功能范围

### 3.1 In Scope

**M1: Mission Model**
- `Mission` 作为 task 的上层抽象
- `MissionPhase` 作为 milestone 的等价物
- `MissionFeature` 作为 hunger item 的结构化扩展
- Mission artifacts: `mission.md / features.yaml / validation-contract.yaml`

**M2: Multi-Worker Scheduling**
- `RuleBasedPlanner` 扩展：从"选 1 个 item"到"选 N 个 item 分配给 M 个 worker"
- Worker dependency: `Assignment.depends_on`
- Sequential execution (fan-out 在 v0.6，join 在 v0.7)
- Worker retry policy

**M3: Structured Handoff**
- `WorkerHandoff` 替代 `WorkerResult`
- `HandoffItem` 表达 blocker / follow-up / discovered issue
- Orchestrator handoff processing loop

**M4: Multi-Stage Validation**
- `ValidationPipeline`: deterministic checks → scrutiny → user-testing
- `ScrutinyValidator`: 运行 test/typecheck/lint，review committed features
- `UserTestingValidator`: 执行 validation-contract assertions
- Validator 作为特殊 worker，自动注入

**M5: Mission Artifacts**
- `mission.md`: mission spec
- `features.yaml`: feature queue with fulfills mapping
- `validation-contract.yaml`: behavioral assertions
- `services.yaml`: service manifest (optional)

**M6: Mission CLI**
- `hungerloop mission new`
- `hungerloop mission run`
- `hungerloop mission status`
- `hungerloop mission features`
- `hungerloop mission validation`
- `hungerloop mission edit`
- `hungerloop mission import`

### 3.2 Out of Scope (v0.7+)

- LLMPlanner
- 真正的并发执行（fan-out + join）
- Cross-task memory recall
- Vector retrieval
- LLM-as-judge validation
- Web UI
- Background daemon

---

## 4. 数据模型变更

### 4.1 Mission 模型

```python
# src/hungerloop/models/mission.py
from pydantic import BaseModel, Field
from datetime import datetime


class MissionPhase(BaseModel):
    """Mission phase (milestone equivalent)."""
    phase_id: str
    title: str
    description: str
    feature_ids: list[str] = Field(default_factory=list)
    validation_contract_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "validating", "done"] = "pending"
    completed_at: datetime | None = None


class MissionFeature(BaseModel):
    """Structured feature (extends HungerItem)."""
    feature_id: str
    hunger_item_id: str  # links to HungerItem
    phase_id: str
    title: str
    description: str

    preconditions: list[str] = Field(default_factory=list)
    expected_behavior: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    fulfills: list[str] = Field(default_factory=list)  # validation contract IDs

    status: Literal["pending", "in_progress", "done", "blocked"] = "pending"
    assigned_worker_ids: list[str] = Field(default_factory=list)


class Mission(BaseModel):
    """Mission (task 的上层抽象)."""
    mission_id: str
    task_id: str  # links to Task
    title: str
    description: str

    phases: list[MissionPhase] = Field(default_factory=list)
    features: list[MissionFeature] = Field(default_factory=list)

    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

### 4.2 WorkerHandoff 模型

```python
# src/hungerloop/models/worker.py (扩展)
from typing import Literal


HandoffItemType = Literal[
    "blocker",
    "follow_up",
    "discovered_issue",
    "incomplete_work",
    "critical_context",
]


class HandoffItem(BaseModel):
    """Structured handoff item."""
    item_type: HandoffItemType
    summary: str
    detail: str = ""
    related_feature_ids: list[str] = Field(default_factory=list)
    related_check_keys: list[str] = Field(default_factory=list)
    requires_orchestrator_action: bool = False


class WorkerHandoff(BaseModel):
    """Structured worker handoff (extends WorkerResult)."""
    # 保留 WorkerResult 的所有字段
    agent_id: str
    task_id: str
    loop_id: int
    summary: str = ""
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)

    llm_call_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)

    error: str | None = None
    error_type: str | None = None
    requires_human: bool = False
    retryable: bool = False

    # 新增 handoff 字段
    handoff_items: list[HandoffItem] = Field(default_factory=list)
    what_was_done: list[str] = Field(default_factory=list)
    what_was_left_undone: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    next_worker_hint: str | None = None
```

### 4.3 Assignment 扩展

```python
# src/hungerloop/models/planning.py (扩展)
class Assignment(BaseModel):
    """Agent assignment within a loop plan."""
    assignment_id: str  # NEW
    agent_id: str
    mission: str
    target_hunger_item_ids: list[str] = Field(default_factory=list)
    target_feature_ids: list[str] = Field(default_factory=list)  # NEW
    allowed_tools: list[str] = Field(default_factory=list)

    # NEW: dependency
    depends_on: list[str] = Field(default_factory=list)  # assignment_ids

    # NEW: retry policy
    max_retries: int = 0
    retry_count: int = 0
```

### 4.4 ValidationContract 模型

```python
# src/hungerloop/models/validation.py (扩展)
class ValidationAssertion(BaseModel):
    """Behavioral assertion in validation contract."""
    assertion_id: str
    phase_id: str
    title: str
    description: str

    check_type: str  # extends AcceptanceCheckType
    params: dict[str, Any] = Field(default_factory=dict)

    evidence_requirements: list[str] = Field(default_factory=list)
    status: Literal["pending", "passed", "failed", "blocked"] = "pending"

    validated_at_loop: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ValidationContract(BaseModel):
    """Mission-level validation contract."""
    mission_id: str
    assertions: list[ValidationAssertion] = Field(default_factory=list)

    def assertions_by_phase(self, phase_id: str) -> list[ValidationAssertion]:
        return [a for a in self.assertions if a.phase_id == phase_id]

    def pending_assertions(self) -> list[ValidationAssertion]:
        return [a for a in self.assertions if a.status == "pending"]

    def phase_is_validated(self, phase_id: str) -> bool:
        phase_assertions = self.assertions_by_phase(phase_id)
        return bool(phase_assertions) and all(
            a.status == "passed" for a in phase_assertions
        )
```

---

## 5. 架构演进

### 5.1 v0.5f 架构（当前）

```
HungerEngine.tick() → should_stop?
  ↓ no
RuleBasedPlanner.plan() → 选 1 个 item → 1 个 Assignment
  ↓
WorkerRuntime.run(ExecutionWorker) → WorkerResult
  ↓
Integrator.integrate() → CandidateState
  ↓
ValidationGate.validate() → ValidationReport
  ↓
CommitManager.apply() → promote or reject
```

### 5.2 v0.6 架构（目标）

```
HungerEngine.tick() → should_stop?
  ↓ no
MissionPlanner.plan() → 选 N 个 features → M 个 Assignments (with deps)
  ↓
WorkerScheduler.execute_assignments() → list[WorkerHandoff]
  ↓ (sequential execution, respect depends_on)
HandoffProcessor.process() → extract blockers/follow-ups
  ↓
Integrator.integrate() → CandidateState
  ↓
ValidationPipeline.validate()
  ├─ DeterministicValidator (existing ValidationGate)
  ├─ ScrutinyValidator (auto-injected when phase enters validating)
  └─ UserTestingValidator (auto-injected when phase is validating)
  ↓
CommitManager.apply() → promote or reject
  ↓
MissionStateUpdater.update() → update features.yaml / validation-contract.yaml
```

### 5.3 关键变化

1. **Planner**: `RuleBasedPlanner` → `MissionPlanner`（仍是 rule-based，但支持多 worker）
2. **Execution**: 单次 `worker_runtime.run()` → `WorkerScheduler.execute_assignments()`（sequential fan-out）
3. **Handoff**: `WorkerResult` → `WorkerHandoff` + `HandoffProcessor`
4. **Validation**: 单阶段 `ValidationGate` → 多阶段 `ValidationPipeline`
5. **State**: 隐式 ledger → 显式 `Mission` + artifacts

---

## 6. MissionPlanner 设计

### 6.1 角色与职责

本节描述的是**提议在 v0.6 引入**的 `MissionPlanner` 设计；**当前 v0.5f 仍是** `RuleBasedPlanner.plan(task_id, loop_id, snapshot, budget)` 由 `HungerEngine.tick()` 触发、且每轮至多产出 1 个 `Assignment`。在该前提下，v0.6 拟把 planner 扩展为 mission-aware 版本。其目标职责为：

- 从 `HungerLedger` 中选择 1..M 个 `HungerItem`（M ≤ `budget.max_workers_per_loop`）
- 为每个选中项生成一个 `Assignment`（包含 `agent_id`、`mission`、`target_hunger_item_ids`、`allowed_tools`）
- 返回 `LoopPlan`，包含 `assignments: list[Assignment]`

### 6.2 输入/输出签名

```python
# v0.6 提议签名（当前代码尚未实现同名符号）
def plan(
    self,
    task_id: str,
    loop_id: int,
    snapshot: HungerSnapshot,
    budget: BudgetAllocation,
    mission: Mission | None = None,           # 新增：v0.6 显式 mission
    prior_handoffs: list[WorkerHandoff] = [], # 新增：上一轮 handoff
) -> LoopPlan:
    """Build a LoopPlan with 1..M assignments for the next loop.

    Args:
        mission: 显式 Mission 对象（v0.6+），包含 features、max_parallel_features 等
        prior_handoffs: 上一轮 WorkerHandoff 列表，用于依赖推断与 blocker 检测

    Returns:
        LoopPlan with assignments (empty if no active items or all blocked)
    """
```

当前 `RuleBasedPlanner.plan` 的实际签名是 `(task_id, loop_id, snapshot, budget) -> LoopPlan`，且当前代码库中**不存在** `MissionPlanner` 符号；因此这里的多参数版本应被理解为 **v0.6 拟新增接口**，不是对现有实现的事实描述。

### 6.3 选择算法

**评分公式**（保持 v0.5f 不变）：

```python
score = item.priority * item.gap_score
```

**选择逻辑**：

1. 过滤 `ledger.active_items()`（状态为 `ACTIVE`，未被 blocker 阻塞）
2. 按 `refinement_tier` 分层，优先选择最低 tier（保持 v0.5f 行为）
3. 在同一 tier 内按 `score` 降序排序
4. 在 v0.6 方案中取前 M 个，其中：
   ```python
   M = min(
       budget.max_workers_per_loop,
       len(候选项),
       mission.max_parallel_features or 1,  # v0.6 新增：mission 级并发上限
   )
   ```

**当前 v0.5f 现实**：`RuleBasedPlanner.plan(...)` 里实际执行的是 `n = min(1, budget.max_workers_per_loop)`，所以即使 budget 允许更多 worker，也仍固定只选 1 个 assignment；`mission.max_parallel_features` 也是 **v0.6 拟新增字段**。
**v0.5f 兼容性**：当 `budget.max_workers_per_loop == 1` 且 `mission is None` 时，v0.6 应退化为单 `Assignment`，与当前 `RuleBasedPlanner` 行为完全一致（PRD §28.11 / M11）。

### 6.4 依赖推断

**保守规则**（v0.6 初版）：

- **Feature preconditions**：若 `mission.features[i].preconditions` 引用 `features[j].id`，则 `Assignment(i)` 依赖 `Assignment(j)`
- **Hunger item produces-consumes**：若 `HungerItem(A)` 的 `produces` 字段包含 `HungerItem(B)` 的 `consumes` 字段中的 artifact，则 `Assignment(B)` 依赖 `Assignment(A)`
- **Prior handoff blockers**：若 `prior_handoffs` 中存在 `HandoffItem(type=blocker, related_item_ids=[X])`，则 `HungerItem(X)` 不可选（标记为 blocked）

**实现约束**（I-10 rule-based requirement compilation）：

- 依赖关系必须通过规则编译生成，不得手工硬编码
- **当前 v0.5f 仍只有** `RuleBasedCompiler.compile(task_id, raw_goal, hints)`，代码中尚无 `RequirementCompiler` 类、`compile_discovered_facts(...)` API，亦未形成 `HungerItem.metadata["dependencies"]` 的现有数据通路；因此这里描述的是 **v0.6 拟新增的编译产物约定**

### 6.5 Worker role 标签

提议在 v0.6 引入 `WorkerRole` 枚举（`executor` / `scrutiny` / `user_testing`），但 `MissionPlanner` 在 v0.6 中**仅产生 `executor` 类型的 `Assignment`**：

```python
assignment = Assignment(
    agent_id=EXECUTION_WORKER_V1.agent_id,  # 固定为 execution_worker_v1
    mission=self._mission_for(item, snapshot.phase, mission),
    target_hunger_item_ids=[item.id],
    allowed_tools=list(EXECUTION_WORKER_V1.allowed_tools),
    role="executor",  # v0.6 新增字段，固定为 executor
)
```

**当前 v0.5f 现实**：`Assignment` 现有字段只有 `agent_id`、`mission`、`target_hunger_item_ids`、`allowed_tools`；并不存在 `role` 字段，也没有 `WorkerRole` 枚举。因此上面的 `role="executor"` 必须理解为 **v0.6 拟新增 schema 变更**。

`scrutiny` 和 `user_testing` 角色的 `Assignment` 生成由 v0.7+ 的 `ValidationPipeline` 负责（PRD §9）。

**不变量引用**：

- **I-3 (check-level commit)**：`MissionPlanner` 不涉及 commit 逻辑，但选择的 `HungerItem` 必须有明确的 `check_keys`，以便后续 `ValidationGate` 验证
- **I-10 (rule-based requirement compilation)**：依赖关系必须通过 `RequirementCompiler` 生成，不得在 `MissionPlanner` 中硬编码

---

## 7. WorkerScheduler 设计

### 7.1 角色与职责

本节同样描述的是**提议在 v0.6 引入**的 `WorkerScheduler`。**当前 v0.5f 仍没有** `WorkerScheduler` 符号；实际逻辑位于 `LoopOrchestrator._run_assignments(...)`，按 `plan.assignments` 顺序逐个构建 `ContextPack`、调用 `worker_runtime.run(...)`，并返回 `list[WorkerResult]`。在此基础上，v0.6 拟抽出独立调度器，负责：

- 接收 `LoopPlan.assignments: list[Assignment]`
- 按依赖关系排序（拓扑排序）
- 顺序执行每个 `Assignment`（v0.6 sequential 模型）
- 当前返回 `list[WorkerResult]`，而 `list[WorkerHandoff]` 是 v0.6 拟新增返回形态

### 7.2 执行模型（sequential in v0.6）

```python
# v0.6 提议伪代码；当前代码尚未实现 execute_assignments(...)
async def execute_assignments(
    self,
    task_id: str,
    loop_id: int,
    assignments: list[Assignment],
    budget: BudgetAllocation,
) -> list[WorkerHandoff]:
    """Execute assignments sequentially with dependency ordering.

    Returns:
        list[WorkerHandoff] (one per assignment, or skipped if upstream failed)
    """
    # 1. 拓扑排序（基于 Assignment.target_hunger_item_ids 的依赖关系）
    sorted_assignments = self._topo_sort(assignments)

    # 2. 顺序执行
    results: list[WorkerHandoff] = []
    for assignment in sorted_assignments:
        # I-8: cost_guard 前检（before invocation）
        self.cost_guard.assert_within_budget(task_id)

        # 执行单个 assignment
        handoff = await self._run_one(task_id, loop_id, assignment, budget)
        results.append(handoff)

        # I-8: cost_guard 后检（after invocation）
        self.cost_guard.assert_within_budget(task_id)

        # 3. 依赖中断检测
        if handoff.error and not handoff.retryable:
            # 上游失败 → 下游 skipped
            downstream = self._get_downstream(assignment, sorted_assignments)
            for dep_assignment in downstream:
                skipped_handoff = WorkerHandoff(
                    agent_id=dep_assignment.agent_id,
                    task_id=task_id,
                    loop_id=loop_id,
                    summary=f"Skipped due to upstream failure: {assignment.agent_id}",
                    error="upstream_failed",
                    error_type="upstream_failed",
                    retryable=True,  # 上游修复后可重试
                    handoff_items=[],
                )
                results.append(skipped_handoff)

    return results
```

上面的调用形状特意对齐当前 `CostGuard` 接口：**当前 v0.5f 的** `CostGuard.assert_within_budget(...)` 只接受 `task_id`，并不接收 `budget` 参数。
**不变量引用**：

- **I-8 (cost ceiling)**：v0.6 设计上要求在每个 `Assignment` 执行前后做双检；**当前 v0.5f 已实现的是** `WorkerRuntime.run(...)` 的执行前检查，以及 `OpenAIModelClient.complete_json(...)` 的每次 attempt 前检查；成功记录 usage 后，`CostGuard.record_llm_usage(...)` 会再次落账并复检
- **I-7 (sandbox isolation)**：`_run_one` 内部通过 `SandboxRunner` 执行 worker，所有路径通过 `path_safety.py` 验证

### 7.3 依赖中断处理

**上游失败 → 下游 skipped**：

- 若 `Assignment(A)` 执行失败（`handoff.error is not None` 且 `handoff.retryable == False`），则所有依赖 `A` 的下游 `Assignment(B, C, ...)` 被标记为 `skipped`
- Skipped handoff 的 `error_type="upstream_failed"`，`retryable=True`（表示上游修复后可重试）
- Skipped handoff 不消耗 LLM/tool budget（仅记录元数据）

**Blocker 传播**：

- 若 `Assignment(A)` 的 `WorkerHandoff` 包含 `HandoffItem(type=blocker, related_item_ids=[X])`，则 `HungerItem(X)` 在下一轮 `MissionPlanner.plan()` 中被标记为 blocked，不可选

### 7.4 v0.5f 兼容性

**单 assignment 退化**：

- 当 `len(assignments) == 1` 时，`execute_assignments` 退化为单次 `_run_one` 调用
- 返回 `list[WorkerHandoff]` of length 1
- 与当前 v0.5f 的 `LoopOrchestrator._run_assignments(...)` 单 assignment 路径行为等价（除了返回类型从 `WorkerResult` 变为 `WorkerHandoff`）

**WorkerResult → WorkerHandoff 映射**：

```python
# v0.5f WorkerResult 字段
WorkerResult(
    agent_id, task_id, loop_id, summary,
    artifact_ids, evidence_ids, claim_ids,
    llm_call_ids, tool_call_ids,
    error, error_type, requires_human, retryable,
)

# v0.6 WorkerHandoff 字段（向后兼容）
WorkerHandoff(
    agent_id, task_id, loop_id, summary,
    artifact_ids, evidence_ids, claim_ids,  # 保留
    llm_call_ids, tool_call_ids,            # 保留
    error, error_type, requires_human, retryable,  # 保留
    handoff_items: list[HandoffItem] = [],  # 新增
)
```

所有 v0.5f 字段在 v0.6 中保留，`handoff_items` 为空列表时等价于 v0.5f 行为。

---

## 8. HandoffProcessor 设计

### 8.1 角色与职责

本节描述的是**v0.6 拟新增**的 `HandoffProcessor`。**当前 v0.5f 代码库中尚不存在** `HandoffProcessor`、`WorkerHandoff`、`HandoffItem` 等符号；当前 runtime 保存的是较轻量的 `WorkerResult`。在该前提下，v0.6 拟由 `HandoffProcessor` 提炼 `list[WorkerHandoff]` 为：

- **Prior handoffs summary**：传递给下一轮 `MissionPlanner` 和 `ContextBuilder`
- **StopReason-less 设计（ADR-008）**：`HandoffProcessor` 通过将 `HandoffItem(type=blocker)` 的 `HungerItem` 标记为 `BLOCKED`，让 `HungerEngine.tick()` 在下一次循环中自行检测 `BLOCKED` 状态并返回 `StopReason.BLOCKED`；HandoffProcessor 本身**不返回任何 StopReason**。
- **Mission state 更新**：将 `HandoffItem(type=discovered_issue)` 注入 `HungerLedger`（通过 `RequirementCompiler`）

### 8.2 处理流程

```python
# v0.6 提议伪代码；当前代码尚未实现同名服务与返回模型
async def process_handoffs(
    self,
    task_id: str,
    loop_id: int,
    handoffs: list[WorkerHandoff],
) -> HandoffProcessingResult:
    """Process worker handoffs and update mission state.

    Returns:
        HandoffProcessingResult(
            prior_handoff_summary: str,       # 传递给 ContextBuilder
            discovered_issues: list[DiscoveredFact],  # 注入 RequirementCompiler
        )
    """
    result = HandoffProcessingResult()

    for handoff in handoffs:
        for item in handoff.handoff_items:
            match item.item_type:
                case "blocker":
                    # 标记对应的 HungerItem 为 BLOCKED;
                    # HungerEngine.tick() 在下一次循环中检测到 BLOCKED 后返回 StopReason.BLOCKED (ADR-008)
                    result.blocked_item_ids.append(item.id)

                case "follow_up":
                    # 记录到 prior_handoff_summary（传递给下一轮）
                    result.prior_handoff_summary += f"\n- Follow-up: {item.description}"

                case "discovered_issue":
                    # 注入 RequirementCompiler（I-10）
                    fact = DiscoveredFact(
                        source="worker_handoff",
                        description=item.description,
                        related_item_ids=item.related_item_ids,
                    )
                    result.discovered_issues.append(fact)

                case "incomplete_work":
                    # 标记相关 HungerItem 为 ACTIVE（保持未完成状态）
                    for item_id in item.related_item_ids:
                        self.repo.update_hunger_item_status(task_id, item_id, "ACTIVE")

                case "critical_context":
                    # 记录到 prior_handoff_summary（高优先级）
                    result.prior_handoff_summary = f"[CRITICAL] {item.description}\n" + result.prior_handoff_summary

    return result
```

**不变量引用**：

- **I-9 (BLOCKED ≠ DONE)**：`HandoffProcessor` 检测 `all_remaining_items_blocked()` 时，必须先于 `is_done()` 检查（与 `HungerEngine.tick()` 的 `StopReason` 优先级一致）
- **I-10 (rule-based requirement compilation)**：`discovered_issue` 在 v0.6 设计上必须通过规则编译器注入 `HungerLedger`，不得直接修改 ledger；**当前 v0.5f 尚无** `compile_discovered_facts(...)` 或 `DiscoveredFact` 这一路径

### 8.3 Discovered issue 注入

**流程**（保持 I-10）：

1. `HandoffProcessor` 将 `HandoffItem(type=discovered_issue)` 转换为 `DiscoveredFact`
2. 调用提议中的 `RequirementCompiler.compile_discovered_facts(task_id, facts)`
3. 由该提议中的编译器根据规则生成新的 `HungerItem`（或更新现有 item 的 `gap_score`）
4. 新 `HungerItem` 自动进入下一轮 `MissionPlanner.plan()` 的候选池

**示例**：

```python
# v0.6 提议中的 Worker handoff
HandoffItem(
    item_type="discovered_issue",
    description="Missing error handling in payment flow",
    related_item_ids=["feature_payment_processing"],
)

# v0.6 提议中的编译结果
HungerItem(
    id="discovered_error_handling_payment",
    description="Add error handling to payment flow",
    priority=8,  # 高优先级（discovered issue 默认 priority=8）
    gap_score=1.0,
    refinement_tier=0,
    check_keys=["test_payment_error_handling"],
    metadata={"source": "worker_handoff", "parent_feature": "feature_payment_processing"},
)
```

### 8.4 ContextBuilder 集成

**新增字段**：`WorkerContext.prior_handoff_summary`（v0.6 拟扩展）

```python
@dataclass
class WorkerContext:
    raw_goal: str
    mission: str
    prior_loop_summary: str          # v0.5f 已有
    prior_handoff_summary: str = ""  # v0.6 新增
    workspace_state: str = ""
    budget_remaining: str = ""
```

**当前 v0.5f 现实**：现有 `ContextBuilder.build_for_agent(...)` 产出的是 `ContextPack`，其中包括 `last_self_summary`、`failure_patterns_to_avoid`、`relevant_evidence_summaries`、`best_workspace_files` 与 `truncation_info`；当前并没有 `prior_handoff_summary` 字段，也没有 handoff-aware 的裁剪路径。因此此处应理解为 **对现有 ContextPack 的提议性扩展**。

**Budget cap 共享**（与 `prior_loop_summary` 一致）：

- `prior_handoff_summary` 与 `prior_loop_summary` 共享同一个 token budget cap（默认 2000 tokens）
- 若两者总长度超过 cap，优先保留 `prior_handoff_summary`（更近期的信息）
- 截断策略：保留最近 N 条 handoff items，丢弃旧的 loop summaries

**v0.5f 兼容性**：

- 当 `prior_handoff_summary == ""` 时，`ContextBuilder` 行为与 v0.5f 完全一致
- v0.5f 代码无需修改，仅在 v0.6 中填充 `prior_handoff_summary` 字段

---

## 9. ValidationPipeline 设计

### 9.1 三阶段流水线与触发边界

v0.6 将当前单阶段 `ValidationGate` 扩展为一个**分层但仍由 hunger 驱动**的 `ValidationPipeline`：

```text
DeterministicValidator
  → ScrutinyValidator
  → UserTestingValidator
```

触发规则必须保持简单且可审计：

1. **DeterministicValidator**：每个 loop 都执行；它直接包装现有 `ValidationGate.validate(...)`，继续承担 target checks + regression checks 的主验证职责（保持 I-5）。
2. **ScrutinyValidator**：仅在 phase 进入 `validating` 状态后触发；若 deterministic 阶段未通过，则不得进入该阶段。
3. **UserTestingValidator**：仅在 ScrutinyValidator 通过后、且 phase 仍处于 `validating` 状态时触发；它消费 `validation-contract.yaml` 中与当前 phase 相关的行为断言。

这里没有新的独立 scheduler。是否进入 scrutiny / user-testing，仍然取决于 `HungerEngine.tick()` 推进后的 phase 状态变化；ValidationPipeline 只是 phase 边界上的一个验证扩展点，而不是新的任务分发器（满足 `VAL-DESIGN-001`）。统一采用 ADR-007 口径：`ScrutinyValidator` 与 `UserTestingValidator` 仅在 phase 进入 `validating` 状态后触发；两者都通过且 deterministic 阶段无新 regression 时，phase 才推进到 `done`；任一失败回退到 `in_progress`。

### 9.2 DeterministicValidator：对 ValidationGate 的零行为包装

`DeterministicValidator` 的目标不是替换 `ValidationGate`，而是把它显式纳入 v0.6 的多阶段流水线中，并保持 **zero behavior change**：

```python
async def validate(
    self,
    task_id: str,
    loop_id: int,
    candidate: Any,
    target_hunger_item_ids: list[str],
) -> ValidationReport:
    ...
```

基于当前代码，`ValidationGate.validate(...)` 的实际语义已经满足 v0.6 需要保留的 deterministic contract：

- 读取 `BestState.accepted_check_keys`，把**目标项 checks**与**先前通过 checks**合并为本轮验证集合；
- 通过 `AcceptanceCheckRunner.run(...)` 对单条 check 做分发；
- 仅当某条 check 新通过时才写入 `newly_passed_check_keys`，回归则进入 `regressed_check_keys`；
- `currently_passed_check_keys` 采用"本轮通过 + 未重跑但此前已通过"的并集语义，从而保持 targeted validation（I-5）。

若 v0.6 在单个 loop 中顺序执行多个 assignment，则传给 `StagnationDetector.update(...)` 的 `attempted_hunger_item_ids` 也必须保持 I-6：只统计本轮**实际尝试执行**的 `target_hunger_item_ids` 并取其并集；被上游失败直接标记为 `skipped` 的下游 assignment 不计入 attempted 集合。

因此 v0.6 中的 DeterministicValidator 应当只是：

```python
class DeterministicValidator:
    def __init__(self, gate: ValidationGate) -> None: ...

    async def validate(...) -> ValidationReport:
        return await self.gate.validate(...)
```

它不改变现有 `AcceptanceCheckType` 枚举，也不改变单条 check 的判定方式。当前代码中的 6 个枚举值必须原样保留：

```text
file_exists
shell_exit_zero
evidence_count_min
artifact_type_exists
human_approval
llm_judge
```

注意：`llm_judge` 虽然是既有枚举成员，但当前 `AcceptanceCheckRunner.run(...)` 对它仍抛出 `NotImplementedError`；因此 v0.6 ValidationPipeline 不能把它作为默认 contract 路径的一部分。

### 9.3 ScrutinyValidator：受限工具集的审查阶段

`ScrutinyValidator` 是一个特殊 validator worker，用来在 deterministic 阶段通过后补充"工程质量审查"，但它必须继续服从 v0.5f 的安全边界。

允许工具集建议固定为：

```text
允许：
- pytest
- mypy
- ruff
- read_file
- read_evidence

禁止：
- write_file
- edit_symbol
- direct_git_write
- arbitrary_network
```

这样做的原因是：scrutiny 的职责是**审查已产出的 candidate**，而不是再次修改 candidate。所有需要启动子进程的命令（如 `pytest`、`mypy`、`ruff`）都必须继续走 `SandboxRunner.run_argv(...)`，继承现有的超时、输出截断、进程组清理与 evidence 落库能力（I-7）。换言之：

```python
async def run_scrutiny_check(...) -> SandboxRunResult:
    return await sandbox_runner.run_argv(
        task_id=task_id,
        loop_id=loop_id,
        argv=["pytest", "tests/..."],
        cwd=candidate_root,
        timeout=timeout,
        evidence_label="scrutiny:pytest",
    )
```

同样，进入 scrutiny 阶段前后都必须执行成本守卫。现有代码已经把 `CostGuard.assert_within_budget(task_id)` 作为 LLM/worker 调用边界的强约束：`WorkerRuntime.run(...)` 在 worker 执行前检查，`OpenAIModelClient.complete_json(...)` 在每次 attempt 前检查，而 `CostGuard.record_llm_usage(...)` 在成功调用后再次落账并校验。因此 v0.6 中的 ScrutinyValidator 必须显式保持"前检 + 后检"语义，不能绕开 I-8。

### 9.4 UserTestingValidator：断言分发表与默认禁用 llm_judge

`UserTestingValidator` 的职责是把 mission 级行为断言映射到确定性的执行器。对于当前已存在的 acceptance checks，优先复用 `AcceptanceCheckRunner`；只有 mission runtime 新增的断言类型，才增加新的 dispatch 分支。

建议分发表如下：

| `check_type` | v0.6 执行路径 | 说明 |
| --- | --- | --- |
| `file_exists` | 复用 `AcceptanceCheckRunner` | 直接检查 candidate workspace 中文件是否存在 |
| `shell_exit_zero` | 复用 `AcceptanceCheckRunner` | 通过 `SandboxRunner` 运行 argv，并保留 shell evidence |
| `evidence_count_min` | 复用 `AcceptanceCheckRunner` | 统计 evidence 数量 |
| `artifact_type_exists` | 复用 `AcceptanceCheckRunner` | 检查 artifact 类型是否存在 |
| `human_approval` | 复用 `AcceptanceCheckRunner` | 查询 approval gate |
| `behavioral_assertion` | v0.6 新增 dispatch | 由 ValidationContract 的结构化断言驱动，可映射到多个确定性子检查 |
| `cli_smoke` | v0.6 新增 dispatch | 在受控 argv 白名单上做 CLI smoke，仍走 `SandboxRunner` |

这里有两个边界需要写清楚：

1. 现有 `AcceptanceCheckType` 中虽有 `llm_judge`，但当前 runner 仍未实现；
2. **v0.6 默认的 validation-contract 不启用 `llm_judge`**，以保持 §0.2 的 deterministic validation 原则，并满足 `VAL-DESIGN-002`。

可将该约束直接写成 contract 规则：

```text
No llm_judge by default.
只有人类显式扩展 ValidationContract 且后续版本提供审计约束时，才允许进入非确定性 judge 路径。
```

### 9.5 兼容性、退化与工作区边界

为了满足 backward compatibility，ValidationPipeline 必须支持"无 contract / 无 phase validator"的退化路径：

```text
若 mission 不存在 validation-contract.yaml：
  - 不注入 ScrutinyValidator
  - 不注入 UserTestingValidator
  - 整个 pipeline 退化为单阶段 DeterministicValidator
  - 对旧任务的行为等价于 v0.5f 的 ValidationGate
```

这保证 v0.5f 任务在 v0.6 runtime 下**零修改运行**，同时也保证新流水线不会改变旧任务的 commit 触发条件（仍然只有 check-level progress 才能提交，见 I-3）。

另外，validator 虽然会读取 candidate workspace 与 evidence，但不应直接写 `best/`。任何通过 scrutiny / user-testing 产生的"通过"结论，最终仍应回流到 `CommitManager.apply(...)` 的既有提交流程，由它调用 `workspace_manager.promote_candidate_to_best(...)` 完成提升，保持工作区隔离（I-4）。也就是说，ValidationPipeline 可以扩展验证层次，但不能绕开 `CommitManager`，也不能直接修改 `best/`。

---

## 10. Mission Artifacts 格式

### 10.1 `mission.md`：人类可读 mission 规格

`mission.md` 的目标是提供一个**稳定、可审阅、适合 handoff 的 Markdown 真相视图**。它不是运行时唯一真源；运行时状态仍以 repository / SQLite 为准，但 worker、validator 与人类审阅者应能仅通过该文件理解 mission 的目标、阶段与约束。

建议结构如下：

```markdown
# Mission: <title>

## Description
- <mission summary>
- <scope and goals>

## Phases
### <phase_id> <title>
- Goals:
  - <goal 1>
  - <goal 2>
- Features:
  - <feature_id>
  - <feature_id>
- Validation assertions:
  - <assertion_id>

## Constraints
- Invariants: I-3, I-4, I-5, I-7, I-8, I-9, I-10
- Backward compatibility: <v0.5f fallback rule>
- Service/runtime limits: <optional summary>

## Notes
- <operator notes>
- <migration notes; see §11>
```

这里的 `Phases / Constraints / Notes` 是文档层 schema，而不是 Python 实现。其设计目标是让**提议在 v0.6 引入**的 `MissionLoader` 或同类解析器可以只做**轻量提取**：读取标题、Description、Phases 列表、Constraints 和 Notes，而不要求 mission.md 承载所有细粒度状态。**当前 v0.5f 代码库中并不存在 `MissionLoader` 符号**；也就是说，`mission.md` 在此处是未来 mission runtime 的长期 spec，而不是当前 event log。

### 10.2 `features.yaml`：feature 队列与状态镜像

`features.yaml` 用于把 `MissionFeature` 队列投影为一个适合机器读写、同时也适合 code review 的 YAML 视图。字段应与 §4.1 的 `MissionFeature` 保持一一对应，但额外暴露 worker/validator 关心的调度状态：

```yaml
features:
  - feature_id: F1.2
    phase_id: M1
    title: Mission Artifacts 格式
    description: >
      Insert section 10 into hungerloop_v0_6_prd.md and cross-reference §4.4 and §11.
    preconditions: []
    expected_behavior:
      - section 10.1 through 10.5 exist
      - YAML/Markdown schemas only; no full implementations
    verification_steps:
      - grep section headers
      - review cross-references
    fulfills:
      - VAL-CONTENT-007
      - VAL-CONTENT-012
    status: in_progress
    assigned_worker_ids:
      - worker-doc-writer
```

该文件是 `MissionFeature` 队列的 **read-only mirror**，不是双向同步源。v0.6 的单一真源是 SQLite：运行时状态更新先落到 repository，成功 commit 后由 `MissionStateUpdater` 从 SQLite 重新生成 `features.yaml`、`mission.md`、`validation-contract.yaml` 与 `services.yaml`。人工修改不得直接反向投影到运行中状态，必须通过 `hungerloop mission edit` 或 `hungerloop mission import` 显式进入 `RequirementCompiler.compile_mission_changes(...)` 路径；import 只写 SQLite，best workspace 中的 YAML/Markdown 镜像会在下一次 commit tail 由 `MissionStateUpdater` 原子替换。该规则采用 ADR-009 的单一真源口径，避免 YAML 与持久化状态漂移。

### 10.3 `validation-contract.yaml`：行为断言契约

`validation-contract.yaml` 是 §4.4 `ValidationAssertion` 模型的人类可读镜像，用于声明 phase 级可验证行为，而不是替代 `ValidationReport`。建议 schema：

```yaml
assertions:
  - assertion_id: VAL-CONTENT-007
    phase_id: M1
    title: Section 10 headers exist
    description: >
      PRD must contain section 10 and subsections 10.1 through 10.5 in order.
    check_type: behavioral_assertion
    params:
      file: hungerloop_v0_6_prd.md
      headers:
        - "## 10. Mission Artifacts 格式"
        - "### 10.1"
        - "### 10.2"
        - "### 10.3"
        - "### 10.4"
        - "### 10.5"
    evidence_requirements:
      - grep_output
      - line_numbers
```

这里的字段必须与 §4.4 `ValidationAssertion` 对齐：`assertion_id`、`phase_id`、`title`、`description`、`check_type`、`params`、`evidence_requirements` 是 artifact 层的最小稳定接口；运行时可在内存/数据库中补充 `status`、`validated_at_loop`、`evidence_ids` 等状态字段，但不要求在 contract 文件中总是展开。

### 10.4 `services.yaml`：可选运行环境清单

`services.yaml` 在 v0.6 中应当是**可选 artifact**，默认可为空或完全缺省。只有 mission 显式声明需要外部服务、端口占用、healthcheck、启动/停止命令时，runtime 才强制读取它。

建议最小 schema：

```yaml
services:
  web:
    start: <command>
    stop: <command>
    healthcheck: <command>
    port: 3000
    depends_on: []

commands:
  test: <command>
  lint: <command>
  typecheck: <command>
```

若 mission 不需要长期进程，则允许：

```yaml
services: []
```

这一定义与 §11 的迁移/验证策略相兼容：旧任务没有 `services.yaml` 时，runtime 仍可按 v0.5f 单任务路径运行；只有 mission 显式进入多 worker / 多 validator 模式时，才把 service manifest 当作操作契约。

### 10.5 Artifact 写入规则：候选区生成，提交后同步

Mission artifacts 必须服从与普通产物相同的工作区边界：worker/validator 只能写入当前 loop 的 candidate workspace，**不得直接修改 `best/`**。这是对 I-4（workspace isolation）的直接延续。

结合现有代码，规则应写明为：

```text
1. worker 在 candidates/loop_NNN/files/ 中生成或更新 mission.md / features.yaml /
   validation-contract.yaml / services.yaml。
2. validator 只读取 candidate workspace 与 evidence；不直接写 best/。
3. 只有 CommitManager.apply(...) 在满足 I-3 后，才调用
   WorkspaceManager.promote_candidate_to_best(task_id, loop_id)。
4. promote 成功后，candidate 中的 mission artifacts 才随同其他文件一起进入 best/。
5. 若 validation 或 commit 失败，candidate artifact 与其他文件一起进入 rejected/，
   不污染 best/。
```

其中第 3 步已有代码基础：`CommitManager.apply(...)` 在可提交时调用 `workspace_manager.promote_candidate_to_best(...)`，随后还会在 repository 事务中持久化 `BestState`、accepted checks，以及 candidate 的 committed/rejected 元数据。换言之，**当前代码里的 commit 路径并不只是文件提升**；它同时更新 repository 状态。因而 v0.6 的 Mission Artifacts 只应作为 candidate 内的人类可读视图，与普通产物共享 promotion 路径；它们不应绕过 `CommitManager`，也不应引入新的直写通道。这样既保持 I-4，也为 §11 的迁移策略保留清晰边界：旧任务即使没有这些 artifacts，仍可沿用原有 best/candidate/rejected 流程运行。

---

## 11. 测试策略

### 11.1 基线与覆盖目标

v0.6 RC 基线为 `pytest tests/` 至少 ≥761 unit + ≥19 integration collected（默认 real-LLM integration 可跳过，但不得有失败或错误）。v0.6 新增能力必须满足：

- 单测：每个新增 service / model 至少 1 个 happy-path + 1 个 edge-case 测试。
- 集成：每个 M1–M6 的端到端流必须有 1 个 `tests/integration/` 用例。
- 回归：v0.5f 既有行为测试全部保持通过，零修改运行（验证 §0.2 的 backward compatible 原则）。

### 11.2 新增测试模块

```text
tests/unit/test_mission_model.py
  - Mission / MissionPhase / MissionFeature 字段约束
  - phase status 转换 (pending → in_progress → validating → done)
  - feature ↔ hunger_item 双向引用一致性

tests/unit/test_worker_handoff.py
  - WorkerHandoff 序列化兼容 WorkerResult 字段
  - HandoffItem 类型枚举完备性 (blocker / follow_up / discovered_issue / incomplete_work / critical_context)
  - requires_orchestrator_action 路由语义

tests/unit/test_mission_planner.py
  - 单 worker fallback (n=1, 等价 RuleBasedPlanner)
  - N 个 feature 分配到 M 个 assignment
  - depends_on DAG 拓扑顺序
  - max_workers_per_loop 上限尊重

tests/unit/test_worker_scheduler.py
  - 顺序执行尊重 depends_on
  - 单个 assignment 失败时下游 assignment 标记为 skipped
  - retry_count 达到 max_retries 后 BLOCKED
  - cost_guard 中断点

tests/unit/test_handoff_processor.py
  - blocker → orchestrator follow-up 队列
  - discovered_issue → 注入新 hunger item
  - critical_context → 写入 cross-loop summary

tests/unit/test_validation_pipeline.py
  - DeterministicValidator 等价 ValidationGate
  - ScrutinyValidator 仅在 phase 进入 validating 状态后注入
  - UserTestingValidator 在 contract assertions pending 时跳过
  - 三阶段顺序：phase 进入 validating 后，按 scrutiny → user-testing 的顺序追加在该 loop 的 deterministic 验证之后执行；两者都通过且 deterministic 无新 regression 时才推进 done，任一失败回退 in_progress

tests/unit/test_validation_contract.py
  - assertions_by_phase / pending_assertions / phase_is_validated
  - assertion status state machine

tests/unit/test_mission_artifacts.py
  - mission.md / features.yaml / validation-contract.yaml 序列化往返
  - artifact 写入路径在 candidate workspace，不污染 best/
  - YAML schema 校验
```

### 11.3 集成测试

```text
tests/integration/test_mission_run_single_worker.py
  - v0.5f 风格任务在 v0.6 runtime 下零修改运行（兼容性闸门）

tests/integration/test_mission_run_multi_worker.py
  - 2 个 feature 串行 fan-out，验证 handoff 链路与 commit 顺序

tests/integration/test_mission_validation_pipeline.py
  - phase 进入 validating 状态时触发 scrutiny + user-testing
  - assertion 失败导致 phase 回退 in_progress

tests/integration/test_mission_resume.py
  - tick 中断后 SQLite 重启，mission/phase/feature 状态完整恢复
```

### 11.4 必须保持的回归

- check-level commit (I-3) 在多 worker 场景仍然成立：任一 commit 必须有 newly_passed_check_keys。
- workspace isolation (I-4) 在并发 assignment 下不退化：每个 assignment 仍然只能写入 `candidates/loop_NNN/`。
- targeted validation (I-5) 在 ValidationPipeline 下不变：deterministic 阶段仍只跑 target + previously-passed。
- attempted-only stagnation (I-6) 在多 assignment 场景下不变：只统计本轮实际尝试执行的 assignment；被上游失败直接 `skipped` 的下游项不计入 attempted 集合。
- BLOCKED ≠ DONE (I-9)：handoff blocker 项必须传播到 hunger item 的 BLOCKED 状态，不得被 phase completion 短路。

### 11.5 工具链门槛

完成提交前必须全部通过：

```bash
pytest tests/                       # ≥761 unit + ≥19 integration collected
mypy --strict src/                  # 严格类型检查
ruff check src/ tests/              # lint
hungerloop --version                # CLI smoke
hungerloop mission --help           # 新 CLI 子命令注册检查
```

---

## 12. 可观测性与遥测

### 12.1 事件扩展

`EventType` 在 v0.5f 已有 task / loop / commit / validation 事件。v0.6 新增：

```text
mission.created
mission.phase_started
mission.phase_validated
mission.phase_completed
mission.feature_assigned
mission.feature_completed
mission.feature_blocked

worker.assignment_started
worker.assignment_completed
worker.handoff_emitted
worker.handoff_received

validation.scrutiny_started
validation.scrutiny_completed
validation.user_testing_started
validation.user_testing_completed
validation.assertion_passed
validation.assertion_failed
```

每个事件必须包含 `mission_id`、`phase_id`（如适用）、`feature_id`（如适用）、`assignment_id`（如适用），以便 trace 重建 mission 轨迹。

### 12.2 Trace 扩展

`LoopTrace` 增加：

- `mission_snapshot`: 该 loop 起始时的 mission state（phase / feature 状态）。
- `assignment_traces`: 每个 assignment 的执行段（开始时间、结束时间、handoff 摘要、cost 开销）。
- `validation_pipeline_trace`: 三阶段 validator 的运行结果。

### 12.3 报告扩展

`hungerloop report` 现有输出添加 mission 视图：

```text
Mission: <mission_id> — <title>
Phases:
  [✓] phase_1 — <title>            validated at loop 12
  [→] phase_2 — <title>            in_progress (3/5 features done)
  [ ] phase_3 — <title>            pending

Features in active phase:
  [✓] feat_2_1   <title>           worker_a (loop 14)
  [→] feat_2_2   <title>           worker_b (loop 15, handoff pending)
  [×] feat_2_3   <title>           BLOCKED — see handoff_items[0]

Validation contract:
  Pending: 4    Passed: 7    Failed: 1    Blocked: 0
```

### 12.4 SQLite schema migration

新增 `v6__mission_runtime.sql` 迁移：

```sql
CREATE TABLE missions (
  mission_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE mission_phases (
  phase_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE mission_features (
  feature_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  phase_id TEXT NOT NULL REFERENCES mission_phases(phase_id),
  hunger_item_id TEXT NOT NULL REFERENCES hunger_items(item_id),
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE worker_handoffs (
  handoff_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE validation_assertions (
  assertion_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  phase_id TEXT NOT NULL REFERENCES mission_phases(phase_id),
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX idx_phases_mission ON mission_phases(mission_id);
CREATE INDEX idx_features_phase ON mission_features(phase_id);
CREATE INDEX idx_assertions_phase ON validation_assertions(phase_id);
CREATE INDEX idx_handoffs_loop ON worker_handoffs(task_id, loop_id);

PRAGMA user_version = 6;
```

迁移走现有 `SQLiteMigrator` 路径，零停机；`InMemoryRepository` 同步增加等价存储字段。

---

## 13. 风险与缓解

| ID | 风险 | 概率 | 影响 | 缓解 |
|----|------|------|------|------|
| R1 | 多 worker 引入隐式并发，破坏 I-4 workspace 隔离 | 中 | 高 | v0.6 仅做 sequential fan-out；并发推迟 v0.7。同一 loop 内的多个 assignment 共享同一 candidate workspace `candidates/loop_NNN/files/`，由 `depends_on` 拓扑顺序串行写入，避免并发冲突。每个 assignment 独立产出 `handoffs/<assignment_id>.json` 用于审计。 |
| R2 | `WorkerHandoff` 替换 `WorkerResult` 破坏 v0.5f 测试 | 中 | 中 | `WorkerHandoff` 是 `WorkerResult` 的超集；保留旧字段 + 新增字段。提供 `WorkerHandoff.as_worker_result()` 适配器。 |
| R3 | mission artifacts (yaml) 与 SQLite ledger 双写不一致 | 中 | 高 | 单一真源：SQLite。yaml 仅作为 candidate workspace 内的可读视图，每次 commit 由 `MissionStateUpdater` 重新生成。 |
| R4 | ValidationPipeline 三阶段串联导致单 loop 时长激增 | 中 | 中 | scrutiny / user-testing 仅在 phase 进入 `validating` 状态后触发，不每 loop 跑。受 cost_guard (I-8) 双闸门保护。 |
| R5 | depends_on DAG 出现环 | 低 | 高 | `MissionPlanner.plan()` 在生成时做拓扑校验；环路视为 planner bug，立即 SAFETY_STOP。 |
| R6 | handoff_items 中的 discovered_issue 无限注入新 hunger item | 低 | 中 | 每个 loop 注入上限 ≤ `budget.max_new_items_per_loop`（默认 3）；超出转为 follow_up，写入 ledger backlog。 |
| R7 | scrutiny validator 跑 lint/test/typecheck 时 sandbox 超时 | 中 | 中 | 复用现有 `SandboxRunner`（I-7）的 timeout / output cap / process-group cleanup；超时记为 assertion blocked，不是 failed。 |
| R8 | mission.md 体积增长污染 cross-loop context window | 中 | 低 | `ContextBuilder` 注入 mission summary 时使用现有 history cap（2000 chars）+ phase-level 截断。 |
| R9 | 旧 task（无 mission）进入 v0.6 runtime 时缺字段 | 高 | 中 | `MissionPlanner` 检测无 mission 时回落到 v0.5f `RuleBasedPlanner` 行为，不强制升级。 |
| R10 | LLM-as-judge 漂入新 validator | 低 | 高 | §0.2 第 5 条原则强制 deterministic validation。CI lint 规则禁止在 `services/validators/` 目录引用 ModelClient。 |
| R11 | `MissionStateUpdater` 重新生成 best artifacts 失败导致 SQLite 状态与 human-readable mirror 不一致 | 低 | 高 | `MissionStateUpdater.regenerate(...)` 使用同目录临时文件 + `os.replace(...)` 原子替换；若 commit tail 再生成失败，则回滚本次 commit 并保留旧 best mirror，不产生半写 artifact。 |

---

## 14. 验收标准

v0.6 release 必须满足以下全部条目，否则不得合并 main：

### 14.1 功能验收

- [ ] **F1** `Mission`、`MissionPhase`、`MissionFeature`、`WorkerHandoff`、`HandoffItem`、`ValidationAssertion`、`ValidationContract` 全部进入 `models/`，通过 `mypy --strict`。
- [ ] **F2** `MissionPlanner` 在 `max_workers_per_loop ≥ 2` 时能输出 ≥ 2 个 assignment 且尊重 `depends_on`。
- [ ] **F3** `WorkerScheduler` 顺序执行 assignments，遇到上游失败时下游标记 skipped，不抛异常。
- [ ] **F4** `HandoffProcessor` 处理 5 种 `HandoffItemType`，对 `requires_orchestrator_action=True` 的项写入 orchestrator 队列。
- [ ] **F5** `ValidationPipeline` 三阶段在 phase 进入 `validating` 状态后按序触发；两者都通过且 deterministic 阶段无新 regression 时，phase 才推进到 `done`；任一失败回退到 `in_progress`。
- [ ] **F6** `mission.md`、`features.yaml`、`validation-contract.yaml` 在每次 commit 后由 `MissionStateUpdater` 重新生成到 best workspace。
- [ ] **F7** 新增 CLI：`hungerloop mission new / run / status / features / validation / edit / import` 全部可用并有 `--help`。

### 14.2 兼容性验收

- [ ] **C1** v0.6 基线 `pytest tests/` 零失败，至少 ≥761 unit + ≥19 integration collected（默认可跳过 real-LLM integration）。
- [ ] **C2** 没有 mission 字段的旧任务（v0.5a–v0.5f 持久化的 SQLite db）能在 v0.6 runtime 启动并继续推进。
- [ ] **C3** SQLite migration v6 在 v5 数据库上向前迁移成功，迁移失败时回滚，不破坏既有数据。
- [ ] **C4** 单 worker 模式（`max_workers_per_loop=1`）行为与 v0.5f 等价（同一任务的 trace diff 仅限于新增字段）。

### 14.3 不变量验收

- [ ] **I1** I-3 check-level commit 在多 worker 流下仍生效：commit 必须有 `newly_passed_check_keys` ≠ ∅。
- [ ] **I2** I-4 workspace isolation 不退化：所有 worker 写入仍限于自己的 candidate 子目录。
- [ ] **I3** I-5 targeted validation 在 deterministic 阶段语义不变。
- [ ] **I4** I-7 sandbox isolation 在 scrutiny validator 调用 lint/test 时仍生效。
- [ ] **I5** I-8 cost guard 在每个 worker invocation 与每个 validator stage 前后各调一次。
- [ ] **I6** I-9 BLOCKED ≠ DONE 在含 handoff blocker 的场景仍生效，phase 不会跳过 BLOCKED feature 直接 done。

### 14.4 质量门槛

- [ ] **Q1** `pytest tests/` 全部通过。
- [ ] **Q2** `mypy --strict src/` 零错误。
- [ ] **Q3** `ruff check src/ tests/` 零警告。
- [ ] **Q4** PR 描述列出所触及的不变量与对应回归测试 ID。

---

## 15. 发布与里程碑

### 15.1 阶段拆分

```text
M1  Mission model + SQLite v6 migration              (1 周)
M2  WorkerHandoff + HandoffProcessor                 (1 周)
M3  MissionPlanner + WorkerScheduler (sequential)    (1.5 周)
M4  ValidationPipeline + ScrutinyValidator           (1 周)
M5  UserTestingValidator + ValidationContract 写入   (1 周)
M6  Mission CLI + 报告扩展                           (0.5 周)
RC  集成测试 + 兼容性回归 + 文档冻结                 (1 周)
```

总周期目标：**7 周**（v0.5f → v0.6.0），不含 v0.7 LLMPlanner 与并发执行。

### 15.2 进入条件 (entry)

- v0.6 RC 基线稳定绿：`pytest tests/` 至少 ≥761 unit + ≥19 integration collected，`mypy --strict src/` clean，`ruff check src/ tests/` clean。
- `report1.md` P0 列表已对齐到 §1.2。
- SQLite v5 迁移在生产任务库上无回归。

### 15.3 退出条件 (exit / GA)

- §14 所有验收项打勾。
- 至少 1 个真实多 worker 任务在沙箱中跑完整 mission 周期，trace + report 可读。
- `RELEASE_CHECKLIST.md` 签收 v0.6 条目。
- v0.7 占位 issue / in-repo placeholder 已开（LLMPlanner、并发 fan-out + join、cross-task memory recall、`services.yaml` rich semantics、Web UI）。

### 15.4 回滚策略

- SQLite v6 → v5 回滚脚本 (`v6_rollback.sql`) 随 PR 一并交付：删除新表，PRAGMA user_version=5。
- Feature flag `HUNGERLOOP_MISSION_RUNTIME=0` 强制走 v0.5f 路径；该旗标 **DEPRECATED, removable in v0.7.0**，仅作为 v0.6 兜底回滚开关保留。

---

## 附录 A. 不变量影响矩阵

| Invariant | v0.5f 实现位置 | v0.6 触及面 | 风险 | 必须验证 |
|-----------|-----------------|------------|------|---------|
| I-3 check-level commit | `commit_manager.py` | 多 worker handoff 后的 commit 路径 | 中 | C1 / I1 + `test_commit_manager` 全套保留 |
| I-4 workspace isolation | `workspace_manager.py` | 同一 loop 内多个 assignment 共享 `candidates/loop_NNN/files/`，并按 `depends_on` 拓扑顺序串行写入 | 中 | I2 + `test_workspace_isolation_multi_worker` |
| I-5 targeted validation | `validation_gate.py` | 作为 `DeterministicValidator` 嵌入 pipeline | 低 | I3 + 现有 `test_validation_gate` 保留 |
| I-6 attempted-only stagnation | `stagnation_detector.py` | 多 assignment 时 attempted 集合并集 | 低 | 现有测试 + 新增 `test_stagnation_multi_assignment` |
| I-7 sandbox isolation | `sandbox_runner.py` | scrutiny validator 调用 lint/test | 中 | I4 + R7 缓解 |
| I-8 cost ceiling | `cost_guard.py` | 每个 assignment + 每个 validator stage | 高 | I5 + `test_cost_guard_pipeline` |
| I-9 BLOCKED ≠ DONE | `hunger_engine.py` | handoff blocker → hunger item BLOCKED | 高 | I6 + `test_blocked_propagation_from_handoff` |
| I-10 rule-based requirement compilation | `requirement_compiler.py` | mission feature ↔ hunger item 映射 | 低 | 现有测试 + `test_feature_compilation_from_policy` |

`StopReason` 优先级 `HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE` 在 v0.6 不变；mission/phase/feature 的状态机不引入新的 `StopReason`。

---

## 附录 B. 术语表

| 术语 | 定义 |
|------|------|
| **Mission** | task 的上层抽象，承载 phases / features / validation contract 的人类可读 spec。一个 task 至多一个 mission；缺失时自动回落 v0.5f 行为。 |
| **MissionPhase** | mission 内的 milestone，包含若干 feature 与 assertion。状态机 `pending → in_progress → validating → done`；`ScrutinyValidator` 与 `UserTestingValidator` 仅在 phase 进入 `validating` 状态后触发，两者都通过且 deterministic 阶段无新 regression 时才推进到 `done`，任一失败回退到 `in_progress`。 |
| **MissionFeature** | hunger item 的结构化外壳，提供 preconditions / expected_behavior / verification_steps / fulfills 等可读字段，并映射到 `HungerItem.item_id`。 |
| **Assignment** | 单 loop 内分配给单个 worker 的执行单元，含 `assignment_id`、`depends_on`、`max_retries`、`target_feature_ids`。 |
| **WorkerHandoff** | `WorkerResult` 的超集，新增 `handoff_items / what_was_done / what_was_left_undone / verification_commands / next_worker_hint`。 |
| **HandoffItem** | 结构化交接条目，类型为 `blocker / follow_up / discovered_issue / incomplete_work / critical_context`，可标记 `requires_orchestrator_action`。 |
| **HandoffProcessor** | 消费 handoff_items 的 service：blocker → hunger BLOCKED；discovered_issue → 注入新 hunger item；critical_context → cross-loop summary。 |
| **MissionPlanner** | rule-based planner 的 v0.6 升级，输出多 assignment 的 `LoopPlan`，仍受 `HungerEngine.tick()` 与 `budget.max_workers_per_loop` 约束。 |
| **WorkerScheduler** | 按 `depends_on` 拓扑顺序执行 assignment 的协调器。v0.6 仅 sequential fan-out，并发 join 推迟 v0.7。 |
| **ValidationPipeline** | 三阶段流水线：`DeterministicValidator → ScrutinyValidator → UserTestingValidator`。后两者仅在 phase 进入 `validating` 状态后触发，且全部 deterministic（不引入 LLM judge）。 |
| **ScrutinyValidator** | 调用项目 lint / typecheck / test 的特殊 worker，复用 `SandboxRunner`，结果作为 ValidationAssertion 状态。 |
| **UserTestingValidator** | 执行 `validation-contract.yaml` 中行为断言的 worker，结果回写 assertion 状态。 |
| **ValidationAssertion** | validation contract 中的单条断言，状态机 `pending → passed / failed / blocked`，关联 evidence_ids。 |
| **ValidationContract** | mission 级别的行为契约集合，`assertions_by_phase` / `pending_assertions` / `phase_is_validated` 为查询接口。 |
| **Mission Artifacts** | 人类可读镜像：`mission.md / features.yaml / validation-contract.yaml / services.yaml`，每次 commit 后由 `MissionStateUpdater` 从 SQLite 重新生成到 best workspace。SQLite 仍是单一真源。 |
| **Sequential Fan-out** | 同一 loop 内串行执行多个 assignment 的模式；和 v0.7 的并发 fan-out + join 区分。 |
| **Backward Compatible** | v0.5f 任务在 v0.6 runtime 零修改运行；`max_workers_per_loop=1` 时与 v0.5f 行为等价。 |

---
