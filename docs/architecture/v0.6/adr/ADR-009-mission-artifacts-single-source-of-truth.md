# ADR-009: Mission Artifacts 的单一真源边界与人工编辑通道

## Status
Status: Accepted (2026-05-23)

## Context

v0.6 PRD §10 在候选工作区引入四个人类可读 artifact：

- `mission.md` — mission spec
- `features.yaml` — feature 队列状态镜像
- `validation-contract.yaml` — 行为断言契约
- `services.yaml` — 可选运行环境清单

§13 R3 + §10.5 已经声明：

> 单一真源：SQLite。yaml 仅作为 candidate workspace 内的可读视图，每次 commit 由 `MissionStateUpdater` 重新生成。

但 §10.2 line 953 同时允许人工反向编辑：

> 该文件应被视为 **MissionFeature ↔ artifact 的双向同步视图**：运行时更新先落到 repository，再由提议中的 `MissionStateUpdater` 回写 artifact；**人工编辑则必须经过显式加载与校验流程**，避免 YAML 与持久化状态漂移。

两段表述在 v0.6 同时存在意味着：
1. 任务运行中 worker 写 candidate；
2. commit 后 `MissionStateUpdater` 把 SQLite 状态投影成 YAML；
3. 同时允许人手编辑 YAML，然后通过"显式加载流程"反写 SQLite。

(3) 让 "SQLite 是单一真源" 退化为 "SQLite 是最终真源"，并引入三个新风险：

- **运行中并发改写**：worker 写 candidate YAML 与人工编辑 best/YAML 没有冲突检测策略；
- **校验 surface 膨胀**：YAML schema 必须双向兼容（读 + 写），而 SQLite 已经是结构化的；
- **审计链断裂**：人工编辑的改动没有 evidence_id，违反 §0.1 / I-3 "evidence is mandatory"。

这与 §0.2 原则 1（hunger-driven，不引入独立 mission scheduler）也冲突——允许人工反写实质上引入了第二条修改 ledger 的路径，绕开 `HungerEngine.tick()` 与 `RequirementCompiler`。

## Decision

v0.6 GA 时，**Mission artifacts 严格降级为 read-only mirror，禁止运行中反向同步**。具体：

### 1. 运行时单向投影

```
SQLite (single source of truth)
   ↓ (CommitManager.apply 成功后)
MissionStateUpdater
   ↓ (regenerate from scratch)
best/mission.md
best/features.yaml
best/validation-contract.yaml
best/services.yaml
```

- `MissionStateUpdater.regenerate(task_id)` 在 `CommitManager.apply(...)` 提升 candidate 成功后被调用，作为同一仓库事务的尾段。
- worker 在 candidate 内写 mission artifacts **仅作为 candidate 自己的草稿**；commit 时由 `MissionStateUpdater` 用 SQLite 状态**完全覆盖**，candidate 草稿不被采纳到 best/。
- 也就是说，artifact 在 best/ 中的内容**完全由 SQLite 状态确定**，与 candidate 中 worker 写了什么无关。

### 2. 人工编辑走单独的 import 子命令

允许操作员修改 mission spec（mission.md / 新增 feature / 新增 validation assertion），但必须走显式 import 通道：

```bash
hungerloop mission edit <task_id>                # 打开编辑窗口（默认 $EDITOR）
hungerloop mission import <task_id> --from <path>  # 显式 import YAML/Markdown
```

import 流程：

1. 任务必须处于 `HUMAN_PAUSED` 状态。runtime 在 active 状态下拒绝 import（返回明确错误）。
2. 读取用户提供的 YAML/Markdown，做 schema 校验。
3. 通过 `RequirementCompiler.compile_mission_changes(task_id, parsed_spec)`（I-10 规则编译路径）生成新的 `HungerItem` / `MissionFeature` / `ValidationAssertion`。
4. 在 repository 事务中写入；产出 `evidence_kind="mission_import"` 的 evidence 用于审计。
5. 操作员 `hungerloop run --resume <task_id>` 时，新的 ledger 自动进入下一轮 `MissionPlanner.plan()`。

import 不直接写 YAML 文件——SQLite 写入后，下一次 commit 才由 `MissionStateUpdater` 重新生成 YAML。这保证 best/ 中的 YAML 永远是 SQLite 的投影。

### 3. 文件位置与原子写入

`MissionStateUpdater.regenerate(...)` 必须：

- 使用 `tempfile.NamedTemporaryFile` 写到同目录 `<name>.yaml.tmp`，然后 `os.replace(...)` 原子替换；避免读取者拿到半写状态。
- 写入路径全部经过 `path_safety.resolve_workspace_path(...)`，不绕过 I-4。
- 写入失败时回退 commit transaction（artifact 投影是 commit 的尾段，失败则视为 commit 失败，candidate 进 rejected/）。

## Alternatives Considered

### A. 保留双向同步（PRD §10.2 原文）
- **Rejected** — 引入运行中并发写 + 第二条 ledger 修改路径，违反 §0.2 原则 1 与 I-10。审计链断裂使得 reproducibility 无法保证。

### B. 完全禁用人工编辑（YAML 只读，不提供 import）
- **Rejected** — 操作员需要能扩展 mission（追加 feature、补充 assertion）；如果只能改 SQLite，操作复杂度过高，且没有人类可读的 diff 视图。提供 `mission import` 既保留可编辑性，也强制走规则编译路径。

### C. 允许 best/ YAML 直接编辑，运行时定期 reconcile
- **Rejected** — reconcile 需要一个 diff/merge 服务，复杂度极高；而且仍然是双源，问题没有真正解决。

### D. Mission artifacts 改为只投影到 candidate（不投影到 best/）
- **Rejected** — 操作员需要在不进入 candidate 工作区的情况下查看当前 mission 状态。best/ 是最稳定的快照，是 mission spec 应该住的地方。

### E. 人工编辑触发自动暂停 → reconcile → 恢复
- **Rejected** — 隐式状态机太多。显式 `mission import` + 要求任务先 `HUMAN_PAUSED` 比"自动检测文件改动"可预测得多。

## Consequences

**Positive**
- "SQLite 是单一真源" 真的成立；运行时不会出现两个 ledger 在打架。
- I-10 (rule-based requirement compilation) 不被绕开：所有 ledger 变更（worker handoff、operator import）都走 `RequirementCompiler`。
- I-3 (evidence is mandatory) 不被绕开：operator import 产出 `mission_import` evidence，可在 trace / report 中追溯。
- §0.2 原则 1 (hunger-driven) 真正成立：`HungerEngine.tick()` 不需要担心 ledger 在 tick 之间被外部修改。
- artifact regeneration 失败会回滚 commit，避免 best/ 与 SQLite 漂移。

**Negative**
- 操作员不能"快速编辑 YAML 跑起来"；必须 pause + import + resume。对运维场景影响很小（mission spec 改动是低频操作），但 PRD 需要在 §12.3 报表与 §11 测试中明确这个工作流。
- candidate 中 worker 写的 mission artifact 草稿被 commit 时丢弃，可能让 worker 的"我修改了 mission.md"行为看起来无效。Mitigation：在 PRD §10.5 显式说明 candidate 内的 artifact 仅作为 worker 的内部草稿，commit 后由 SQLite 状态决定 best/ 内容。
- `MissionStateUpdater.regenerate(...)` 失败成为 commit 失败的新路径，需要在 §13 风险表追加一条。

**Trade-offs**
牺牲 "随手编辑 YAML 就能改 mission" 的轻便性，换 "SQLite 永远是单一真源 + 所有 ledger 变更都有 evidence + I-10 永远生效" 的可审计性。对于 hunger-driven agent harness 来说，这笔账明显划算。

## Compliance

- PRD §10.2 line 953 必须改写：移除 "双向同步视图" 表述；改为 "单向投影：SQLite → YAML mirror，由 `MissionStateUpdater` 在 commit 尾段重新生成"。
- PRD §10.5 line 1019 不变；新增一段说明 candidate 内 mission artifact 只是 worker 草稿，commit 后由 `MissionStateUpdater` 完全覆盖。
- PRD §13 风险表追加：
  | R11 | `MissionStateUpdater.regenerate` 失败导致 best/ artifact 与 SQLite 漂移 | 低 | 高 | 投影作为 commit 事务尾段，失败回滚整个 commit；写入用 `os.replace(...)` 原子替换 |
- PRD §3.1 (in scope) 追加：`hungerloop mission import` / `hungerloop mission edit` CLI 子命令。
- PRD §14.1 追加验收项 **F8**：在任务 `RUNNING` 状态下调用 `hungerloop mission import` 必须返回错误码并拒绝写入；只有 `HUMAN_PAUSED` 状态下才接受 import。
- 新增单测：
  - `tests/unit/test_mission_state_updater.py` — regenerate 必须用 `os.replace(...)`；regenerate 失败时整个 commit 事务回滚。
  - `tests/integration/test_mission_import_paused_only.py` — running 任务 import 拒绝；paused 任务 import 成功并生成 evidence。
  - `tests/unit/test_artifact_single_source.py` — 在 candidate 写一个不同的 features.yaml，commit 后 best/features.yaml 必须等于 `MissionStateUpdater` 用 SQLite 投影出来的内容，不等于 candidate 草稿。
- `MissionStateUpdater` 必须**禁止**反向读 YAML 到 SQLite 的 API。CI lint 规则禁止在 `services/mission_state_updater.py` 中引用 `yaml.safe_load` 之外的 `yaml.load` / `yaml.unsafe_load`，且禁止调用 `repo.update_*(...)`。
