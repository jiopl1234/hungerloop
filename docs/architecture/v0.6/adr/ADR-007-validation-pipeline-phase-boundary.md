# ADR-007: ValidationPipeline 触发边界与 MissionPhase 状态机所有权

## Status
Status: Accepted (2026-05-23)

## Context

v0.6 PRD §9 引入 `ValidationPipeline`，将 v0.5f 单阶段 `ValidationGate` 扩展为三层：

```
DeterministicValidator → ScrutinyValidator → UserTestingValidator
```

PRD §4.1 声明 `MissionPhase.status` 状态机为：

```
pending → in_progress → validating → done
```

但触发边界在多处出现口径不一致：

- §9.1 line 754：`ScrutinyValidator` 在 phase 进入 `done` 边界时触发一次。
- §11.2 line 1075 / §14.1 F5 line 1271：`ScrutinyValidator` 在 phase done 时注入。
- §11.3 line 1099：phase 进入 done 边界时触发 scrutiny + user-testing。

如果 `done` 是 scrutiny / user-testing 通过之后的结果，那么"phase 进入 done 后触发 scrutiny"是因果倒置；如果 `done` 只是名义状态，则 §11.3 的"assertion 失败导致 phase 回退 in_progress"无法落地（已是 done 的 phase 如何回退？）。

这同时影响：

- I-3 (check-level commit)：commit 必须在 ValidationPipeline 通过之后才能发生，因此 phase 不能在 commit 之前进入 `done`。
- I-9 (BLOCKED ≠ DONE)：phase done 不能短路 BLOCKED feature。
- §0.2 原则 1 (hunger-driven)：phase 状态推进必须由 `HungerEngine.tick()` 驱动，不引入独立 phase scheduler。

## Decision

**`validating` 是 ValidationPipeline 的执行窗口；phase 状态推进权归 `HungerEngine.tick()`。** 具体：

1. 当一个 phase 内**所有 `MissionFeature.status == "done"`** 时（即 deterministic 阶段已让所有目标 check 通过并 commit），`HungerEngine.tick()` 将该 phase 推进到 `validating`。
2. 进入 `validating` 边界触发 `ScrutinyValidator`。`ScrutinyValidator` 通过后触发 `UserTestingValidator`。
3. **两者都通过且本轮没有新的 deterministic regression** 时，`HungerEngine.tick()` 在下一轮将 phase 推进到 `done`。
4. 任一 validator 失败时：
   - 受影响的 `ValidationAssertion.status` 被标记为 `failed` 或 `blocked`；
   - phase 回退到 `in_progress`；
   - `HandoffProcessor` 将失败信息编译为新的 `HungerItem`（走 I-10 规则编译路径），由下一轮 `MissionPlanner.plan()` 重新调度。

PRD §9.1 / §11.2 / §11.3 / §14.1 F5 / 附录 B `MissionPhase` 词条必须改写为以下统一表述：

> ScrutinyValidator 与 UserTestingValidator 仅在 phase 进入 `validating` 状态后触发；两者都通过且 deterministic 阶段无新 regression 时，phase 才推进到 `done`；任一失败回退到 `in_progress`。

`DeterministicValidator` 仍然每个 loop 都跑，不受 phase 状态影响——它是 `ValidationGate` 的零行为包装，承担 I-5 targeted validation。

```python
# v0.6 提议伪代码：phase 推进点统一在 HungerEngine.tick()
def tick(self, task_id: str, ...) -> TickResult:
    snapshot = self.repo.get_hunger_snapshot(task_id)
    mission = self.repo.get_mission(task_id)

    # ... 现有 stop-reason 优先级检查 ...

    for phase in mission.phases:
        if phase.status == "in_progress" and self._all_features_done(phase):
            phase.status = "validating"

        if phase.status == "validating" and self._pipeline_passed(phase):
            phase.status = "done"

        if phase.status == "validating" and self._pipeline_failed(phase):
            phase.status = "in_progress"
```

## Alternatives Considered

### A. ScrutinyValidator 在 phase 进入 `done` 后触发
PRD 原文写法。
- **Rejected** — 与 §11.3 "assertion 失败回退 in_progress" 矛盾。已经 done 的 phase 没有定义的"回退"语义；如果允许 done → in_progress 回退，那么 `done` 不再是终态，会污染 `mission.is_completed()` 判定与所有依赖 `phase.status == "done"` 的下游分支（features 报表、UI、CLI 输出）。

### B. ValidationPipeline 自己维护 phase 状态机
让 `ValidationPipeline.run(...)` 在三阶段全部通过后直接调用 `repo.update_phase_status(phase_id, "done")`。
- **Rejected** — 破坏 §0.2 原则 1 (hunger-driven)。这会产生两个 phase scheduler（`HungerEngine` 处理 task-level stop，`ValidationPipeline` 处理 phase-level done），下游服务无法知道应该信哪一个。同时使得 `StopReason` 优先级判定无法集中在 `HungerEngine.tick()`。

### C. 不要 `validating` 状态，直接在 `in_progress` 里跑 ValidationPipeline
phase 始终在 `in_progress`，所有 features 都 done 后才一次性进 `done`。
- **Rejected** — 失去可观测性。CLI / 报表无法区分 "feature 写完但 validation 还没跑" 与 "validation 正在跑" 两种状态；scrutiny / user-testing 的 traces 也没有清晰的归属期。`validating` 状态的存在本身就是为了让操作员能在 mission status 上看到 "正在审查"。

### D. ScrutinyValidator 在每个 loop 都跑
不限定 phase 边界。
- **Rejected** — §13 R4 已经说明这会让单 loop 时长激增。Scrutiny 跑 `pytest` / `mypy` / `ruff` 的耗时跟 deterministic check 不是一个量级，每 loop 跑会让 I-8 cost guard 在大多数任务上提前触发，并且 deterministic 阶段还没通过就跑 scrutiny 也没意义。

## Consequences

**Positive**
- ValidationPipeline 只负责"执行 + 报告"，不负责"决定 phase 走向"；与 §0.2 原则 1 一致。
- `phase.status == "done"` 是真正的终态；任何下游服务可以无歧义地依赖它。
- `StopReason` 优先级仍然只在 `HungerEngine.tick()` 一处计算（I-9 不被绕开）。
- §11.3 的 "assertion 失败回退 in_progress" 有了清晰落地点：`validating → in_progress` 是合法转移；`done → in_progress` 不是。
- I-3 不被绕开：commit 仍然在 deterministic 阶段触发，scrutiny / user-testing 只能阻止 phase 推进，不会绕过 `CommitManager.apply(...)` 直接发布产物。

**Negative**
- `MissionPhase.status` 状态机增加一个不直观的 "all features done but phase not done yet" 中间态。需要在 §12.3 的 `hungerloop report` 输出中显式区分 `[→]` `validating` vs `[✓]` `done`。
- `HungerEngine.tick()` 复杂度上升：需要在每轮 tick 内查询 mission / phase / features 状态。可以通过 `RepositoryProtocol.get_phase_with_features(phase_id)` 一次性拉取来摊销。
- 单 phase 的 `validating` 窗口可能跨多个 loop（scrutiny 跑 pytest 可能本身就需要一轮 cost guard 配额），下游报表需要能显示 "validating since loop N"。

**Trade-offs**
拒绝把 phase 状态推进权下放给 ValidationPipeline，本质上是用一点状态机复杂度（多一个 `validating` 中间态）换取**调度入口的唯一性**。`HungerEngine.tick()` 仍然是 v0.6 中唯一推进任务状态的服务。

## Compliance

- §9.1 / §11.2 / §11.3 / §14.1 F5 / 附录 B `MissionPhase` 必须按"`validating` = 执行窗口；`done` = scrutiny + user-testing 通过后的终态"统一改写。
- `ValidationPipeline` 实现中**不允许**调用 `repo.update_phase_status(...)`；只允许更新 `ValidationAssertion.status` 与写 evidence。
- `HungerEngine.tick()` 必须保留 `StopReason` 优先级 `HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`，并在 phase 推进逻辑之前完成 stop 决策。
- 新增单测 `tests/unit/test_phase_state_machine.py`：
  - `in_progress + 所有 features done → validating`
  - `validating + pipeline pass → done`
  - `validating + assertion failed → in_progress`
  - `done → in_progress` 必须抛出非法转移异常
- `done` 状态被持久化到 SQLite `mission_phases.status`；ValidationPipeline 与 CommitManager 都不应直接写这个字段。
