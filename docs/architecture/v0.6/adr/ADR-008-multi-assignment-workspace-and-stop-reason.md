# ADR-008: 多 Assignment 工作区布局与 StopReason 所有权归属

## Status
Status: Accepted (2026-05-23)

## Context

v0.6 PRD §6–§8 把单 worker 调度演进为多 assignment sequential fan-out。两件事在 PRD 中存在内部冲突：

### 冲突 1：单 candidate workspace 还是 per-assignment 子目录

- §13 R1 缓解措施：
  > v0.6 仅做 sequential fan-out；并发推迟 v0.7。**每个 assignment 仍有独立 candidate 子目录**。
- §10.5 line 1019–1020：
  > worker 在 `candidates/loop_NNN/files/` 中生成或更新 mission.md / features.yaml / validation-contract.yaml / services.yaml。

两份描述对应两种完全不同的工作区布局：
1. `candidates/loop_NNN/files/`（共享）：所有 assignment 写同一目录，后写覆盖先写。
2. `candidates/loop_NNN/assignments/<assignment_id>/files/`（隔离）：每个 assignment 独立目录，需要在 commit 时合并。

任何一种都行，但**v0.6 必须只走一条**，否则：
- `WorkspaceManager.promote_candidate_to_best(...)` 不知道该提升哪个根；
- `CommitManager.apply(...)` 的事务边界不明确；
- I-4 (workspace isolation) 在 "两个 assignment 写同一文件" 时悄悄退化。

### 冲突 2：StopReason 决定权归 HandoffProcessor 还是 HungerEngine

PRD §8.2 `HandoffProcessor.process_handoffs(...)` 返回 `HandoffProcessingResult(early_stop_reason: StopReason | None, ...)`，并在伪代码中直接给 `early_stop_reason = StopReason.BLOCKED`。

但 v0.5f 的 `HungerEngine.tick()` 已经在一处集中计算 `StopReason` 优先级（`HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`），并通过 `all_remaining_items_blocked()` 判定 BLOCKED。

如果 `HandoffProcessor` 也能产出 `StopReason`，等于有两个服务都能"决定停"：
- 优先级判定要在两处保持一致；
- BLOCKED 与 HUMAN_PAUSED 同时发生时的顺序无法定义；
- 测试覆盖面翻倍。

这直接违反 I-9 (BLOCKED ≠ DONE) 设计精神——`HungerEngine.tick()` 是唯一的 stop 仲裁者。

## Decision

### 决策 1：v0.6 sequential fan-out 共享同一个 candidate workspace

```
workspace/tasks/<task_id>/candidates/loop_NNN/
  ├── files/                  ← 所有 assignment 顺序写入
  ├── evidence/
  └── handoffs/<assignment_id>.json   ← 每个 assignment 自己的结构化 handoff
```

理由：

- **sequential 已经消除了并发冲突**。`WorkerScheduler.execute_assignments(...)` 按 `depends_on` 拓扑串行执行，同一时刻只有一个 assignment 在写 `files/`。
- 文件级冲突由 `depends_on` 在 planner 层避免：若 assignment A 与 B 写同一文件，则 A 必须声明在 B 的 produces / B 的 consumes 中（I-10 规则编译），从而 B 等 A 完成。
- 不需要 commit 时的 merge 逻辑，`WorkspaceManager.promote_candidate_to_best(...)` 接口与 v0.5f 完全一致。
- 仍然满足 I-4：写入边界仍由 `path_safety.resolve_workspace_path(...)` 强制限定在 `candidates/loop_NNN/files/` 之内；rejected candidate 整体进入 `rejected/loop_NNN/`，不污染 `best/`。

per-assignment 隔离推迟到 **v0.7 真正的 fan-out + join** 时再做（届时再写一份新的 ADR）。

§13 R1 的"每个 assignment 仍有独立 candidate 子目录"措辞必须改为：

> v0.6 仅做 sequential fan-out；并发推迟 v0.7。同一 loop 内的多个 assignment **共享同一 candidate workspace** `candidates/loop_NNN/files/`，由 `depends_on` 拓扑顺序串行写入，避免并发冲突。每个 assignment 独立产出 `handoffs/<assignment_id>.json` 用于审计。

### 决策 2：HandoffProcessor 不直接返回 StopReason；只写 HungerItem.status

`HandoffProcessor` 在遇到 `HandoffItem(type=blocker, related_item_ids=[X])` 时：

1. 通过 `RepositoryProtocol.update_hunger_item_status(task_id, X, "BLOCKED")` 把对应 `HungerItem` 标记为 BLOCKED；
2. 调用 `RequirementCompiler.compile_discovered_facts(...)`（I-10）编译相关 follow-up；
3. **不计算 `StopReason`**；返回类型不再包含 `early_stop_reason` 字段。

`HungerEngine.tick()` 在**下一轮开始时**自然通过 `all_remaining_items_blocked()` 检查，发现所有 active items 都进 BLOCKED 后，按既有优先级返回 `StopReason.BLOCKED`。

PRD §8.2 的 `HandoffProcessingResult` schema 改为：

```python
@dataclass
class HandoffProcessingResult:
    prior_handoff_summary: str
    discovered_issues: list[DiscoveredFact]
    blocked_item_ids: list[str]   # 仅用于 observability，不参与 stop 决策
```

## Alternatives Considered

### A. 每个 assignment 独立 candidate 子目录（PRD §13 R1 原文）
- **Rejected for v0.6** — 增加 `WorkspaceManager.merge_assignments(...)` 复杂度，且 sequential fan-out 不需要这种隔离。延后到 v0.7 fan-out + join 时再做，届时合并语义有真正的需求（并发写同一文件）。

### B. HandoffProcessor 立即触发 StopReason，HungerEngine.tick() 信任它
- **Rejected** — 两个 stop 仲裁者会导致 `StopReason` 优先级在两处计算。最容易出问题的场景：HandoffProcessor 标记 BLOCKED 后，operator 在同一 loop 末发 HUMAN_PAUSED。两者哪个先生效？把仲裁权集中在 `HungerEngine.tick()` 才能保持 I-9 与 v0.5f 行为一致。

### C. 同 loop 内每个 assignment 都独立调一次 HungerEngine.tick()
- **Rejected** — 破坏 "loop = tick 单位" 的语义；每 assignment 一次 tick 会让 `clock.loop_count` 与 `drive_budget` 计算紊乱。`tick()` 应保持 loop-level 粒度。

### D. HandoffProcessor 写入 `task_state.early_stop_reason` 字段，HungerEngine 读这个字段优先返回
- **Rejected** — 等价于决策 B，只是绕了一个字段。还是两个仲裁者。

## Consequences

**Positive**
- I-4 (workspace isolation) 在多 assignment 下不退化：所有写入仍受 `path_safety` 限定在同一 candidate 工作区。
- I-9 (BLOCKED ≠ DONE) 不被绕开：`StopReason` 决定权仍然只在 `HungerEngine.tick()` 一处。
- `WorkspaceManager` / `CommitManager` 在 v0.6 不需要新增 merge 逻辑；改动量极小。
- `HandoffProcessor` 的职责变得单一：消费 handoff_items → 更新 ledger + 编译 follow-up；不参与 stop。
- 与 ADR-007 形成闭环：phase 状态推进、StopReason 决定、commit 三件事都集中在 `HungerEngine.tick()` 一处。

**Negative**
- 多 assignment 中"立刻应该 BLOCKED 全任务"的场景，最快也要等下一轮 `tick()` 才生效（一个 loop 的延迟）。对 v0.6 的预期负载（每 loop 10 秒级）来说可忽略。
- 拓扑级文件冲突仍需依赖 `MissionPlanner` 正确生成 `depends_on`。如果 planner bug 让 A、B 都不依赖对方又都写同一文件，后写覆盖先写。Mitigation：在 `WorkerScheduler._run_one(...)` 后做"本 loop 内同 path 写入次数 > 1" 的 sanity 断言，记入 evidence。
- per-assignment 隔离推迟到 v0.7，意味着 v0.6 不支持真正的并发 fan-out。这与 PRD §3.2 (out of scope) 一致，不是回归。

**Trade-offs**
- 决策 1 用"v0.7 再补隔离"换"v0.6 实现简单 + 不引入 merge 风险"。
- 决策 2 用"BLOCKED 信号延迟一轮 tick"换"stop 仲裁单点 + 与 v0.5f 行为一致"。

## Compliance

- PRD §13 R1 必须重写为"共享同一 candidate workspace + 串行写入"。
- PRD §10.5 line 1019 不变（已经声明 `candidates/loop_NNN/files/`），但需补一句："多 assignment 同 loop 写入按 `WorkerScheduler` 拓扑顺序串行执行"。
- PRD §8.2 `HandoffProcessingResult` 移除 `early_stop_reason` 字段；新增 `blocked_item_ids: list[str]` 作为 observability 字段。
- `HungerEngine.tick()` 中现有 `StopReason` 优先级判定保持不变；新增 docstring 注明 "v0.6 多 assignment 场景下，BLOCKED 状态通过 `HungerItem.status` 持久化，由下一轮 tick 通过 `all_remaining_items_blocked()` 检测，不由 `HandoffProcessor` 直接触发"。
- `WorkerScheduler._run_one(...)` 必须在 `path_safety.resolve_workspace_path(...)` 之外，再做一次 per-loop 写入路径去重断言（违反则记录 `evidence_kind="workspace_collision"`）。
- 新增单测：
  - `tests/unit/test_workspace_isolation_multi_assignment.py` — 多 assignment 共享 workspace 但写入路径不重叠时不退化。
  - `tests/unit/test_blocked_propagation_from_handoff.py` — `HandoffProcessor` 标记 BLOCKED 后，下一轮 `tick()` 返回 `StopReason.BLOCKED`，但 `HandoffProcessor` 自身不返回 `StopReason`。
