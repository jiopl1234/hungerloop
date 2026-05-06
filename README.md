# HungerLoop v0.5b/c

基于"check 级别提交"和"饥饿度预算"的 Python 异步 Agent 迭代循环框架。提供工作区隔离、成本守卫、可恢复运行和完整可观测性。

> **状态**：v0.5b/c — SQLite 持久化 CLI + trace/report 加固版本。CLI 默认打开 `hungerloop.sqlite`，支持跨进程恢复 dummy 运行，完整保留 v0.4 的全部不变量。

---

## 目录

1. [核心概念](#核心概念)
2. [功能矩阵](#功能矩阵)
3. [安装](#安装)
4. [5 分钟快速开始](#5-分钟快速开始)
5. [完整工作流程](#完整工作流程)
6. [CLI 使用教程](#cli-使用教程)
7. [验收规范文件](#验收规范文件)
8. [模型配置](#模型配置)
9. [不变量参考](#不变量参考)
10. [项目结构](#项目结构)
11. [开发与测试](#开发与测试)
12. [文档与路线图](#文档与路线图)

---

## 核心概念

HungerLoop 是一个把 Agent 长任务"编译"成可观察、可中断、可恢复迭代循环的运行时。它不试图让模型一次完成任务，而是把任务拆成一组**验收检查（acceptance check）**，每一轮（loop）尝试通过其中一项或多项，并按以下原则约束：

- **饥饿度（Hunger）**：每个任务有总成本/Token/循环数预算，称为"饥饿值"。模型每消耗资源就饿一点，饿完则进入 `HUNGER_EXPIRED` 终止状态。可以通过 `hungerloop hunger refill` 喂饱它继续跑。
- **Check 级别提交（I-3）**：候选状态只有在**确实让某个未通过的 check 变为通过**且**不引发回归**时才会被提交进 `best/`。永远不基于"分数"提交。
- **工作区隔离（I-4）**：模型只能读 `best/`，写到 `candidates/loop_NNN/`。只有 `CommitManager` 能把 candidate 提升为 best。
- **成本守卫（I-8）**：每次 LLM/工具调用前后都校验预算，超额立即 `SAFETY_STOP`。
- **BLOCKED ≠ DONE（I-9）**：所有 hunger item 都被人类阻塞 ≠ 任务完成。停止原因有严格优先级：`HUMAN_PAUSED → SAFETY_STOP → BLOCKED → HUNGER_EXPIRED → DONE`。

完整不变量见[不变量参考](#不变量参考)。

---

## 功能矩阵

| 模块 | 状态 | 说明 |
| ---- | ---- | ---- |
| `LoopOrchestrator` | ✅ | 完整 hunger → plan → execute → validate → commit 循环（PRD §12） |
| `RuleBasedPlanner` | ✅ | 基于 `priority × gap_score` 的规则规划器（§5） |
| `WorkerRuntime` + `ExecutionWorker` | ✅ | `BudgetGuard`、副作用门禁、`ToolNotPermitted`（§6/§7/§28.11） |
| `DummyModelClient` / `OpenAIModelClient` | ✅ | 重试、JSON 安全、`Retry-After`、错误证据落库（§11.4 / §28.2 / §28.3） |
| `ModelConfig` + `PricingTable` | ✅ | YAML 配置，禁明文 key，仅环境变量（§10 / §11.3） |
| `MemoryManager` | ✅ | 每轮生成 `MemoryCandidate`，确定性谓词（§19） |
| `SkillManager` | ✅ | `DONE` 且 ≥2 个 check 通过时发卡（§20） |
| `SQLiteRepository` | ✅ | 前向迁移、WAL、usage_snapshots、task_locks、events、traces、reports、memory、skill |
| `LearningWorker` / `ResearchWorker` | ⏳ | v0.5d |
| `LLMPlanner` + 多 Worker 调度 | ⏳ | v0.6 |
| `Azure OpenAI` 运行时 | ⏳ | 占位实现，调用时显式失败 |
| 长期记忆生产化推广流程 | ⏳ | 后续版本 |

---

## 安装

要求 Python 3.11+。

```bash
# 克隆并安装（含开发工具链）
git clone <repo>
cd hungerloop
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 验证
hungerloop --version
pytest tests/        # 应通过 500+ 测试
mypy --strict src/   # 应零错误
ruff check src/ tests/
```

---

## 5 分钟快速开始

```bash
# 1. 写一个验收规范
cat > accept.yaml <<'YAML'
core_acceptance_checks:
  - check_type: file_exists
    params: {path: report.md}
    description: report.md 必须存在
YAML

# 2. 创建任务（持久化到 ./hungerloop.sqlite）
hungerloop new "生成一份小报告" --accept-file accept.yaml --task-id demo-1

# 3. 跑起来（默认 dummy 模型，无成本，可重复）
hungerloop run demo-1

# 4. 查看进度
hungerloop status demo-1

# 5. 查看人类可读报告
hungerloop report demo-1

# 6. 导出完整事件追踪（JSONL，可灌进 Grafana/jq 分析）
hungerloop trace export demo-1 --format jsonl
```

完整 demo 任务在 `examples/demo_task.yaml`，集成测试 `tests/integration/test_orchestrator_dummy_done.py` 演示了和 CLI 同等装配的端到端跑法。

---

## 完整工作流程

```
                  ┌──────────────────────────────────────────────────────┐
                  │                Task Lifecycle                        │
                  └──────────────────────────────────────────────────────┘

  hungerloop new ──► [pending]
                      │
                      │  hungerloop run <task_id>
                      ▼
                   [running] ◄──────────────────────────────┐
                      │                                     │
        ┌─────────────┼─────────────┐                       │
        │             │             │                       │
        ▼             ▼             ▼                       │
   每一轮 loop：   ┌────────────────────────────────┐       │
   1. HungerEngine.tick()  → 选择 stop 原因         │       │
   2. RuleBasedPlanner    → 选 hunger item           │       │
   3. ExecutionWorker     → 调模型/工具，写候选     │       │
   4. ValidationGate      → 跑目标 check + 回归 check│       │
   5. CommitManager       → 满足 I-3 才提交          │       │
   6. MemoryManager       → 抽取 MemoryCandidate     │       │
   7. SkillManager        → DONE+≥2 check 时发卡    │       │
                          └────────────────────────────────┘
                      │
                      │  达到停止条件
                      ▼
                  [stopped]
                      │
              StopReason 之一：
              ├─ DONE              → hungerloop report
              ├─ HUNGER_EXPIRED    → hungerloop hunger refill --loops N
              ├─ BLOCKED           → hungerloop hunger unblock <item_id>
              ├─ SAFETY_STOP       → hungerloop run --raise-cost-ceiling
              └─ HUMAN_PAUSED      → hungerloop run --resume
                      │
                      │  人工干预后
                      ▼
                   [running]  ◄────────────── （回到循环）
```

**关键持久化语义**：

- 整个状态都在 `./hungerloop.sqlite`（默认路径）；可设 `--db /path/to/other.sqlite` 覆盖。
- 工作区文件（`best/`、`candidates/loop_NNN/`）默认在 `./workspace/<task_id>/`，由 `WorkspaceManager` 管理。
- `task_locks` 表保证同一时刻只有一个 `hungerloop run` 占用某个 task；进程崩溃后超过 `HUNGERLOOP_LOCK_STALE_SEC`（默认 1800 秒）自动可被 `--steal-lock` 接管。

---

## CLI 使用教程

默认情况下，CLI 在**当前工作目录**打开 `hungerloop.sqlite`（通过 `_default_context()`）。要使用其他位置，请 `cd` 到目标目录再运行；`hungerloop checks` 是唯一额外接受 `--db PATH` 显式覆写的子命令（v0.4.1 遗留）。

### `hungerloop new` — 创建任务

```bash
hungerloop new "<目标描述>" \
  [--task-id <id>] \
  [--accept '<json check>' ...] \
  [--accept-file <path.yaml|.json>] \
  [--memory-consolidation]
```

- `--task-id`：不指定则自动生成 UUID。
- `--accept`：可重复，单条 JSON 形式的 check（`{"check_type":"file_exists","params":{"path":"x.md"}}`）。
- `--accept-file`：从 YAML/JSON 文件加载（推荐）。详见[验收规范文件](#验收规范文件)。
- `--memory-consolidation`：启用记忆候选生成。

### `hungerloop run` — 执行循环

```bash
hungerloop run <task_id> \
  [--max-loops N] \
  [--model-config model.yaml] \
  [--refill N]                  # 创建后立即喂饱 N 轮饥饿度
  [--unblock-all]               # 解除所有 BLOCKED item
  [--resume]                    # 从 HUMAN_PAUSED 恢复
  [--raise-cost-ceiling]        # 提高一次成本上限（SAFETY_STOP 后用）
  [--steal-lock]                # 抢占陈旧锁
  [--lock-stale-sec SEC]        # 自定义陈旧阈值（默认 1800）
```

**resume 预检（preflight）**：每次 `run` 启动前会检查 task 当前状态、上次 stop 原因、锁是否陈旧，并把检查结果落库为事件。如果状态不允许恢复（如 `HUMAN_PAUSED` 但未传 `--resume`），CLI 会用清晰提示退出。

### `hungerloop status` — 查看任务状态

```bash
hungerloop status <task_id>
```

输出当前阶段、最近 hunger snapshot（饥饿值、剩余循环、累计成本/Token）、最新 stop_reason、accepted check 数量。

### `hungerloop report` — 人类可读报告

```bash
hungerloop report <task_id> [--format text|json]
```

- `text`（默认）：摘要 + acceptance check 表 + 最近 N 轮决策。
- `json`：完整结构化输出，适合 CI 消费。

### `hungerloop trace export` — 导出事件追踪

```bash
hungerloop trace export <task_id> --format jsonl|json
```

导出整个任务生命周期事件流（`loop_started`、`loop_committed`、`loop_rejected`、`safety_stop`、`human_required` 等），用于离线分析、告警接入或回归调查。

### `hungerloop hunger ...` — 饥饿度操作

```bash
hungerloop hunger refill <task_id> --loops N    # 喂饱 N 轮
hungerloop hunger unblock <task_id> <item_id>   # 解除单条 BLOCKED
hungerloop hunger unblock-all <task_id>         # 解除所有 BLOCKED
hungerloop hunger freeze <task_id>              # 冻结（不再消耗饥饿度）
hungerloop hunger resume <task_id>              # 解冻
```

### `hungerloop memory list` — 列出记忆候选

```bash
hungerloop memory list <task_id> [--state candidate|approved|rejected]
```

显示 `MemoryManager` 抽取的候选记忆（事实/流程/偏好/陷阱），可按生命周期状态过滤。每条候选带确定性谓词标记（`action_verified`、`reusable`、`non_volatile`、`traceable`）。

### `hungerloop skill list` — 列出技能卡

```bash
hungerloop skill list [<task_id>]
```

不传 task_id 列出全部技能卡。技能卡只在任务 `DONE` 且至少 2 个 check 通过时才生成。

### `hungerloop workspace ...` — 工作区检查

```bash
hungerloop workspace best <task_id> [--root workspace]
hungerloop workspace candidate <task_id> --loop N [--root workspace]
hungerloop workspace rejected <task_id> --loop N [--root workspace]
```

列出工作区目录中 best 状态、特定 loop 的候选或被拒文件清单。

### `hungerloop checks` — 验收 check 状态

```bash
hungerloop checks <task_id> [--db PATH]
```

显示每个 acceptance check 当前是否通过、最近一次断言的循环号、关联的 evidence id。

### `hungerloop repair-state` — 状态修复

```bash
hungerloop repair-state <task_id> [--apply] [--scope all|hunger|workspace] [--no-events]
```

检测内存模型 vs SQLite blackboard 之间的偏差（divergence），默认 dry-run 模式。仅在 `--apply` 时实际写入修复。退出码：0=无偏差，1=有偏差但未修复，2=已修复。

---

## 验收规范文件

`--accept-file` 接受 YAML 或 JSON，根键 `core_acceptance_checks` 是数组，每项需要 `check_type` 和 `params`：

```yaml
core_acceptance_checks:
  - check_type: file_exists
    params:
      path: report.md
    description: report.md 必须存在

  - check_type: shell_exit_zero
    params:
      argv: ["python", "-c", "open('report.md').read(); print('ok')"]
      timeout: 10
    description: report.md 必须可读
```

支持的 `check_type`：`file_exists`、`shell_exit_zero`、`http_status`、`regex_match` 等。完整清单见 `services/validation_gate.py`。所有 shell 类 check 都通过 `SandboxRunner` 跑（路径白名单 + 进程组清理 + 超时强制）。

---

## 模型配置

`run --model-config model.yaml`：

```yaml
provider: openai            # dummy | openai | azure_openai（azure 占位）
model_name: gpt-4o-mini
api_key_env: OPENAI_API_KEY # 仅允许指明环境变量名，不允许明文
base_url: null              # 可选自定义 endpoint
pricing:
  input_per_1k_usd: 0.00015
  output_per_1k_usd: 0.0006
retry:
  max_attempts: 3
  initial_backoff_sec: 1.0
```

**安全规则（强制）**：YAML 不允许写 `api_key:` 明文，只能指定 `api_key_env: <ENV_VAR_NAME>`。Azure OpenAI 在调用时显式抛错（v0.5b/c 范围外）。

`dummy` provider 不需要 key，用于本地确定性回归。

---

## 不变量参考

| ID | 名称 | 落地位置 |
| -- | ---- | -------- |
| I-3 | Check 级别提交，永不基于分数 | `commit_manager.py`、`hunger_update.py` |
| I-4 | 工作区隔离：只有 `CommitManager` 写 `best/` | `workspace_manager.py` |
| I-5 | 目标验证 + 回归：先前通过的 check 仍要重测 | `validation_gate.py` |
| I-6 | 停滞检测仅计算 `attempted` item | `stagnation_detector.py` |
| I-7 | 沙箱隔离：路径白名单 + 进程组清理 | `sandbox_runner.py`、`path_safety.py` |
| I-8 | 成本守卫：每次调用前后都校验预算 | `cost_guard.py` |
| I-9 | `BLOCKED ≠ DONE`；停止原因严格优先级 | `hunger_engine.py` |
| I-10 | 饥饿度账本由 policy 编译生成 | `requirement_compiler.py` |

违反任何一条都属于 regression，不属于 refactor。详见 `CLAUDE.md`。

---

## 项目结构

```
src/hungerloop/
  models/         # Pydantic 冻结快照模型；不要往里加可变方法
  services/       # 无状态服务；统一通过 DI 拿 repo
  repository/     # Protocol + InMemoryRepository + SQLiteRepository + 迁移
    migrations/   # v1__initial.sql、v2__memory_candidate_lifecycle.sql、
                  # v3__sqlite_runtime_tables.sql、v4__memory_candidate_sources.sql
  cli/            # click 入口：new、run、status、report、trace、
                  # hunger、memory、skill、workspace、checks、repair-state
tests/
  unit/           # 单元测试
  integration/    # 端到端 orchestrator 测试
examples/
  demo_task.yaml  # 确定性 demo 任务
docs/
  architecture/   # 架构图与决策记录
  superpowers/    # 实现计划归档
```

---

## 开发与测试

```bash
# 全量测试
pytest tests/

# 严格类型检查（必须零错误）
mypy --strict src/

# Lint
ruff check src/ tests/

# CLI 烟测
hungerloop --version
hungerloop new "smoke test" --accept-file examples/demo_task.yaml --task-id smoke
hungerloop run smoke
```

**关键约定**：

- 每个模块顶部加 `from __future__ import annotations`。
- 公共 API 完整类型注解；用 `X | None` 而非 `Optional[X]`。
- 模型用 Pydantic v2，但 `pydantic.mypy` 插件**已禁用**（与 mypy ≥1.18 不兼容）；不要重新打开。
- I/O 或子进程相关服务方法用 `async`；`pytest-asyncio` 在 `auto` 模式。
- 提交风格：`feat:`、`fix:`、`docs:`、`refactor:`、`test:`，参考 `git log`。
- 数据库迁移**前向（forward-only）**：永远不修改已发布的 vN.sql 文件，只能追加 vN+1.sql（PRD §5.5）。

---

## 文档与路线图

**文档**：

- `hungerloop_v0_5b_c_prd.md` — v0.5b/c 产品需求
- `hungerloop_v0_5_2_prd.md` — v0.5.2 产品需求
- `HungerLoop_MVP_PRD_v0.4.1_engineering_fix.md` — v0.4.1 基线
- `CLAUDE.md` — 不变量、约定、MCP 工具用法
- `RELEASE_CHECKLIST.md` — 发布前验证步骤

**路线图**：

- **v0.5c**：记忆推广 CLI、更多技能触发器
- **v0.5d**：`LearningWorker`、`ResearchWorker`
- **v0.6**：`LLMPlanner`、多 Worker 调度、可选并行执行

---

## License

MIT
