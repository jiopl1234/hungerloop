# HungerLoop v0.5d/e PRD — Revised After Review

**版本**: v0.5d/e revised  
**文档类型**: implementation-compatible PRD  
**基线**: v0.5a + shipped v0.5b/c partial implementation  
**修订原因**: 根据最新 review 修正 v0.5d/e PRD 中的前置条件、schema 冲突、EventType 命名冲突、MemoryCandidate 生命周期冲突，以及对已 shipped 工作的重复描述。  
**核心原则**: 不重写已 shipped wire contract；只做兼容性增量。

---

## 0. 一句话目标

v0.5d/e 的目标是：

> 让 HungerLoop 可解释、可恢复、可复盘、可审查沉淀。每个任务可以被 replay，每个停止原因有恢复路径，每个成功/失败都留下可审计的 Memory/Skill candidate。

v0.5d/e 不让 Agent 更“聪明”，而是让系统更可靠：

```text
可观测
可恢复
可报告
可审查
可沉淀
不污染长期记忆
```

---

## 1. Review 决策摘要

本次 review 指出原 v0.5d/e PRD 方向正确，但存在三个关键问题：

```text
C1: PRD 假设 SQLiteRepository 已作为 CLI 默认 backend，但实际尚未满足。
C2: PRD 的 MemoryCandidate v2 schema 与 c0-01 已 shipped schema 冲突。
C3: PRD 的 EventType vocabulary 重命名了已 shipped enum，破坏 wire contract。
```

因此本版做如下修正：

| Review 问题 | 修正决策 |
|---|---|
| SQLiteRepository 前置未满足 | 新增 v0.5b.2 作为 hard gate，v0.5d.0 不能绕过 |
| MemoryCandidate `id/status` 与 shipped `candidate_id/state` 冲突 | 保留 shipped 字段：`candidate_id` + `state` |
| EventType 命名冲突 | 保留 shipped enum 名称，只做 additive additions |
| v0.5d.0 看起来像绿地实现 | 改为 delta plan：标注 shipped / partial / new |
| CLI 仍读 repo 私有字段 | 明确列为 v0.5d.0 要修复的既有违规 |
| SkillManager 引用缺失 protocol 方法 | 补入 RepositoryProtocol |
| LoopTrace/StopReport schema 变更 | 明确为 forward-compatible additive fields |
| Memory reusable blacklist 误伤 | 改为 regex anchored patterns |
| SQLite migration 未命名 | 明确 migration 文件名和 LATEST_VERSION bump |
| Trace 在 worker exception 时可能缺失 | 加入测试与 orchestrator finally path 要求 |
| v3 migration 同时改 schema + index | 拆为 v3 (table) + v4 (indexes); v0.5e 升到 v5 |
| v5 migration 重复 add `expires_at` 列 | 明确 v2 已 ship `expires_at`; v5 不再 ADD |
| `accepted_check_keys` 与 shipped `referenced_check_keys` 重复 | 两者并存；语义分工写入 §14.4 |
| `MemoryType` shipped 值未对齐 | 保留 shipped 4 个值，additive 加 3 个新值 |
| SkillCard 派生算法不确定 | 给出每个 derive_* 的伪代码 + golden test |
| ERROR resume 缺少具体信号 | 要求 `repair_state_action` event 时间戳晚于 ERROR stop_report |
| 进程重启 "状态必须保持" 太模糊 | 列出 10 项必须一致的字段 |
| `WORKER_STARTED` vs `LOOP_STARTED` 边界模糊 | 固定 per-loop event 触发顺序 |

---

## 2. 当前 shipped 状态对照

本 PRD 不再假设 v0.5d/e 是从零开始。根据 review，以下能力已经部分或完全 shipped：

| 模块 | 状态 | v0.5d/e 处理 |
|---|---|---|
| Trace export | shipped / partial | 保留，补协议方法与 event completeness |
| Report CLI v1 schema | shipped / partial | 保留，升级为 StopReport v2 输出 |
| repair-state D1-D7 | shipped | 保留，v0.5d.1 增加 D8+ 与 `--apply` |
| EventType enum | shipped | 保留已 shipped 名称，只 additive 扩展 |
| cost reconciliation | shipped / partial | 保留，接入 UsageSnapshot / Report |
| task lock + release | shipped / partial | 保留，补 SQLiteRepository parity |
| forward-only migrator | shipped | 保留，新增 v3/v4 migrations |
| Memory lifecycle SQL c0-01 | shipped / partial | 不重命名字段，PRD 对齐 shipped schema |
| SQLiteRepository | not shipped / blocking | 新增 v0.5b.2 hard gate |
| CLI default SQLite backend | not shipped / blocking | v0.5b.2 必须完成 |

---

## 3. Release Gate: v0.5b.2 必须先完成

### 3.1 为什么需要 v0.5b.2

原 PRD 将 `SQLiteRepository 可作为 CLI 默认 backend` 作为 v0.5d/e 前提，但实际代码尚未满足。若不先补齐，以下验收不可测：

```text
Orchestrator 实际写 event
Trace/Report 通过 SQLiteRepository 持久化
Memory/Skill 通过 SQLiteRepository 持久化
CLI 重启后状态可恢复
RepositoryProtocol parity
```

因此新增：

```text
v0.5b.2 = SQLiteRepository implementation + protocol parity tests
```

这是 v0.5d.0 的 hard gate。

### 3.2 v0.5b.2 必须交付

```text
1. SQLiteRepository 实现 RepositoryProtocol 的全部现有方法。
2. SQLiteRepository 实现 v0.5d/e 新增观测查询方法。
3. CLI 默认 context 使用 SQLiteRepository，不再抛 v0.5a-only error。
4. InMemoryRepository 与 SQLiteRepository 通过同一套 protocol parity tests。
5. SQLiteRepository 使用 forward-only migrator。
6. SQLite 使用 WAL。
7. 同一 task_id 禁止多进程同时 run。
8. status/report/trace 不读 repo 私有字段。
```

### 3.3 v0.5b.2 验收

```bash
hungerloop new "demo task" --accept-file accept.yaml
hungerloop run <task_id> --model-config dummy.yaml
hungerloop status <task_id>
hungerloop report <task_id>
hungerloop trace export <task_id> --output trace.jsonl
```

退出进程后再运行：

```bash
hungerloop status <task_id>
hungerloop run <task_id> --resume
```

"状态必须保持" 的具体含义 — 第二次进程读到的值必须等于第一次进程写入的值：

```text
1. tasks.task_id, status, last_stop_reason
2. best_states.state_id (most recent BestState for task)
3. accepted_checks (full set of (task_id, check_key) tuples)
4. hunger_clocks.loop_count, hunger, frozen
5. hunger_items.status / consecutive_failure_count for each item
6. usage_snapshots.total_tokens, total_cost_usd, llm_calls, tool_calls
7. last loop_traces.loop_id and committed flag
8. last stop_reports row (stop_reason, goal_status, total_loops)
9. task_locks released cleanly (no dangling owner from process 1)
10. events log appended (no rows lost across restart)
```

If any of (1)-(10) drift across restart, v0.5b.2 is incomplete. Add a
``test_sqlite_repository_restart_parity`` integration test that
exercises this end-to-end against a real on-disk SQLite file (not
in-memory).

---

## 4. Scope: v0.5d/e 不做什么

v0.5d/e 不做：

```text
1. 不做 3×3 Agent。
2. 不做 LLMPlanner。
3. 不做 LLM-as-judge。
4. 不做自动长期记忆晋升。
5. 不做 vector database。
6. 不做 Web UI。
7. 不做 background daemon。
8. 不重命名已 shipped EventType。
9. 不重命名已 shipped MemoryCandidate.candidate_id。
10. 不把 MemoryCandidate.state 改成 status。
```

---

# Part I — v0.5d Observability & Recovery

## 5. v0.5d 目标

v0.5d 解决：

```text
任务发生了什么？
每轮有什么 delta？
为什么停止？
花了多少钱？
哪些 check 新通过？
哪些 check 回退？
哪些工具失败？
哪些模型错误？
状态是否一致？
如何 resume / refill / unblock / repair？
```

---

## 6. RepositoryProtocol additions

### 6.1 原则

v0.5d 的一个核心修复是：**CLI 和 repair/report/trace 不能读取 repository 私有字段。**

Review 已指出现有代码有这些违规：

```text
report_format.py 读取 _stop_reports_history
trace_cmd.py 读取 _events
repair_state.py 读取 _task_locks / _candidates / _accepted_checks / _validation_reports
```

v0.5d 要把这些改为正式协议方法。

### 6.2 新增 Protocol 方法

```python
class RepositoryProtocol(Protocol):
    # Task basics
    def task_exists(self, task_id: str) -> bool: ...
    def get_task_status(self, task_id: str) -> str | None: ...
    def set_task_status(self, task_id: str, status: str) -> None: ...

    # Events
    def append_event(self, event: EventRecord) -> None: ...
    def list_events(
        self,
        task_id: str,
        *,
        since_loop: int | None = None,
        until_loop: int | None = None,
        event_types: list[str] | None = None,
        include_global: bool = False,
    ) -> list[EventRecord]: ...

    # Trace
    def save_loop_trace(self, trace: LoopTrace) -> None: ...
    def get_loop_trace(self, task_id: str, loop_id: int) -> LoopTrace | None: ...
    def list_loop_traces(
        self,
        task_id: str,
        *,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[LoopTrace]: ...

    # Stop reports
    def save_stop_report(self, report: StopReport) -> None: ...
    def get_last_stop_report(self, task_id: str) -> StopReport | None: ...
    def list_stop_reports(self, task_id: str) -> list[StopReport]: ...

    # Hunger snapshot
    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None: ...
    def get_latest_hunger_snapshot(self, task_id: str) -> HungerSnapshot | None: ...

    # Usage
    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> None: ...
    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot: ...

    # Candidates / validation / accepted checks
    def list_candidates_for_task(self, task_id: str) -> list[CandidateState]: ...
    def get_candidate(self, candidate_state_id: str) -> CandidateState | None: ...
    def iter_accepted_checks(self, task_id: str) -> list[AcceptedCheckRecord]: ...
    def validation_exists(self, validation_id: str) -> bool: ...
    def get_validation_report(self, validation_id: str) -> ValidationReport | None: ...

    # Locks
    def get_task_lock(self, task_id: str) -> TaskLock | None: ...

    # Errors
    def list_model_errors(self, task_id: str, *, limit: int | None = None) -> list[dict]: ...
    def list_tool_errors(self, task_id: str, *, limit: int | None = None) -> list[dict]: ...

    # Tool evidence helpers
    def list_successful_tool_call_evidence(self, task_id: str) -> list[Evidence]: ...

    # Memory / skill
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None: ...
    def get_memory_candidate(self, candidate_id: str) -> MemoryCandidate | None: ...
    def list_memory_candidates(self, task_id: str | None = None) -> list[MemoryCandidate]: ...

    def save_promoted_memory(self, memory: PromotedMemory) -> None: ...
    def list_promoted_memories(self) -> list[PromotedMemory]: ...

    def save_skill_card_candidate(self, card: SkillCardCandidate) -> None: ...
    def get_skill_card_candidate(self, skill_candidate_id: str) -> SkillCardCandidate | None: ...
    def list_skill_card_candidates(self, task_id: str | None = None) -> list[SkillCardCandidate]: ...

    def save_active_skill_card(self, card: ActiveSkillCard) -> None: ...
    def list_active_skill_cards(self) -> list[ActiveSkillCard]: ...
```

### 6.3 Protocol parity tests

v0.5b.2 / v0.5d 必须增加：

```text
test_repository_protocol_parity.py
```

同一套测试跑：

```text
InMemoryRepository
SQLiteRepository
```

至少覆盖：

```text
task_exists
list_events
loop_traces
stop_reports
usage_snapshot
task_locks
candidates
accepted_checks
validation_exists
memory candidates
skill candidates
```

---

## 7. EventType: additive only

### 7.1 原则

EventType 已经是 wire contract。  
v0.5d 不允许重命名已 shipped event values。

### 7.2 已 shipped 名称必须保留

已 shipped 的事件名，例如：

```text
LOCK_STOLEN
HUMAN_UNBLOCKED_HUNGER_ITEM
MEMORY_CANDIDATE_EMITTED
SKILL_CARD_EMITTED
```

必须保留。

即使 PRD 更喜欢 `TASK_LOCK_STOLEN` / `MEMORY_CANDIDATE_CREATED`，也不能替换已持久化名字。

### 7.3 v0.5d additive events

新增事件只能 additive：

```python
class EventType(str, Enum):
    # Shipped names remain.

    LOOP_PLANNED = "loop_planned"

    WORKER_STARTED = "worker_started"
    WORKER_FINISHED = "worker_finished"
    WORKER_FAILED = "worker_failed"

    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_SUCCEEDED = "model_call_succeeded"
    MODEL_CALL_FAILED = "model_call_failed"
    MODEL_AUTH_REQUIRED = "model_auth_required"
    MODEL_RATE_LIMITED = "model_rate_limited"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_SUCCEEDED = "tool_call_succeeded"
    TOOL_CALL_FAILED = "tool_call_failed"

    VALIDATION_STARTED = "validation_started"
    VALIDATION_FINISHED = "validation_finished"

    CHECK_PASSED = "check_passed"
    CHECK_FAILED = "check_failed"
    CHECK_REGRESSED = "check_regressed"

    CANDIDATE_CREATED = "candidate_created"
    CANDIDATE_COMMITTED = "candidate_committed"
    CANDIDATE_REJECTED = "candidate_rejected"

    STOP_REPORT_CREATED = "stop_report_created"
    ERROR = "error"
```

### 7.4 Orchestrator event requirements

v0.5d 必须确保 Orchestrator 实际 append：

```text
LOOP_PLANNED
WORKER_STARTED
WORKER_FINISHED / WORKER_FAILED
CANDIDATE_CREATED
VALIDATION_STARTED
VALIDATION_FINISHED
CANDIDATE_COMMITTED / CANDIDATE_REJECTED
STOP_REPORT_CREATED
```

如果已有 `LOOP_STARTED` / `LOOP_COMMITTED` / `LOOP_REJECTED` shipped 名称，则继续使用 shipped 名称，不新增重复语义。

### 7.5 Per-loop event ordering

To prevent double-counting between `LOOP_STARTED` and `WORKER_STARTED`,
the firing order is fixed. One loop iteration emits, in order:

```text
LOOP_STARTED              (once per loop, at orchestrator entry)
LOOP_PLANNED              (once, after RuleBasedPlanner returns)
WORKER_STARTED            (once per worker invocation; can repeat)
  TOOL_CALL_STARTED       (per tool invocation within the worker)
  TOOL_CALL_SUCCEEDED|FAILED
  MODEL_CALL_STARTED      (per LLM call within the worker)
  MODEL_CALL_SUCCEEDED|FAILED
WORKER_FINISHED|FAILED    (once per worker invocation)
CANDIDATE_CREATED         (once, after worker writes candidate workspace)
VALIDATION_STARTED        (once)
CHECK_PASSED|FAILED|REGRESSED  (per check evaluated)
VALIDATION_FINISHED       (once)
CANDIDATE_COMMITTED|REJECTED  (once; mutually exclusive)
LOOP_COMMITTED|LOOP_REJECTED  (once; mirrors the candidate decision)
```

A loop that exits via `SAFETY_STOP` / `HUMAN_REQUIRED` / `ERROR`
short-circuits this sequence; whichever events have already fired
remain in the log, and the orchestrator emits the stop event followed
by `STOP_REPORT_CREATED`. The trace persistence path (§8.3) ensures
a `LoopTrace` row is written even on the short-circuit branch.

---

## 8. LoopTrace v2

### 8.1 Compatibility rule

LoopTrace v2 只做 forward-compatible additions：

```text
新增字段必须有默认值。
SQLite payload_json 必须可承载新增字段。
如果已有列不足，新增列放入 migration v3。
旧 trace 必须能反序列化。
```

### 8.2 Schema additions

```python
class LoopTrace(BaseModel):
    # Existing fields remain.

    best_state_id_before_loop: str | None = None
    best_state_id_after_loop: str | None = None
    verdict: str | None = None

    currently_passed_check_keys: list[str] = Field(default_factory=list)
    satisfied_hunger_item_ids: list[str] = Field(default_factory=list)
    unsatisfied_hunger_item_ids: list[str] = Field(default_factory=list)

    model_errors: list[str] = Field(default_factory=list)
    tool_errors: list[str] = Field(default_factory=list)
    worker_errors: list[str] = Field(default_factory=list)

    stop_reason: StopReason | None = None
```

If not already present, keep / add:

```python
newly_passed_check_keys: list[str] = Field(default_factory=list)
regressed_check_keys: list[str] = Field(default_factory=list)
tokens_consumed_this_loop: int = 0
cost_this_loop_usd: float = 0.0
llm_calls_this_loop: int = 0
tool_calls_this_loop: int = 0
candidate_workspace_ref: str | None = None
```

### 8.3 Required test

Add:

```text
synthetic worker exception -> LoopTrace still persisted
```

Expected:

```text
LoopTrace.stop_reason == ERROR
LoopTrace.worker_errors non-empty
StopReport exists
candidate workspace rejected or safely archived
```

This closes the review concern that the orchestrator can crash before trace write.

---

## 9. StopReport v2

### 9.1 Compatibility rule

StopReport v2 additions are forward-compatible:

```text
New fields default to [] / None / 0.
goal_status remains existing GoalStatus enum.
Do not downgrade goal_status to str.
```

### 9.2 Schema additions

```python
class StopReport(BaseModel):
    # Existing fields remain.

    final_best_state_id: str | None = None

    accepted_check_keys_count: int = 0
    accepted_check_keys: list[str] = Field(default_factory=list)

    total_loops: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0

    last_validation_report_id: str | None = None
    last_loop_id: int | None = None

    recommended_next_actions: list[str] = Field(default_factory=list)
    resume_hint: str | None = None
```

Keep:

```python
recommendation: str
```

Do not rename it to only `recommended_next_actions`; both can coexist.

### 9.3 Resume hints

`STOP_REASON_HINTS` is keyed by `StopReason.value`, not enum name.

```python
STOP_REASON_HINTS = {
    "done": "Task is complete. Use --reset to start a new run.",
    "hunger_expired": "Refill hunger before resuming.",
    "blocked": "Unblock hunger items before resuming.",
    "human_required": "Resolve the requested human action, then run with --resume.",
    "human_paused": "Resume hunger or pass --resume.",
    "safety_stop": "Raise cost/token ceiling before resuming.",
    "error": "Inspect trace/report, repair state, then resume or reset.",
}
```

---

## 10. UsageSnapshot

### 10.1 Goal

UsageSnapshot gives status/report one place to read usage.

### 10.2 Schema

```python
class UsageSnapshot(BaseModel):
    task_id: str

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0

    llm_calls: int = 0
    tool_calls: int = 0

    model_errors: int = 0
    tool_errors: int = 0

    last_loop_id: int | None = None
```

### 10.3 Migration

Persisted by `migrations/v3__usage_snapshots.sql` (full DDL in §11.2).
The v3/v4 split keeps the table addition out of the same transaction
as the index DDL, so a failed index doesn't roll back the new table.

---

## 11. SQLite migrations for v0.5d

### 11.1 One semantic per migration

Splitting schema additions from index optimisations means a failed
index DDL doesn't roll back the table, and the operator can land each
half independently:

```text
migrations/v3__usage_snapshots.sql       (new table)
migrations/v4__observability_indexes.sql (indexes only)
```

Bumps (sequential):

```python
# After v0.5d.0 ships:
SQLiteMigrator.LATEST_VERSION = 4
# v0.5e adds v5 (see §20).
```

The c0-01 v2 migration (memory lifecycle columns) already shipped, so
v3/v4 do **not** touch ``memory_candidates``.

### 11.2 v3 — usage_snapshots table

`migrations/v3__usage_snapshots.sql`:

```sql
CREATE TABLE IF NOT EXISTS usage_snapshots (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  total_tokens INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_cost_usd REAL NOT NULL DEFAULT 0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  model_errors INTEGER NOT NULL DEFAULT 0,
  tool_errors INTEGER NOT NULL DEFAULT 0,
  last_loop_id INTEGER
);

PRAGMA user_version = 3;
```

### 11.3 v4 — observability indexes

`migrations/v4__observability_indexes.sql`:

```sql
CREATE INDEX IF NOT EXISTS idx_events_task_loop
ON events(task_id, loop_id);

CREATE INDEX IF NOT EXISTS idx_events_task_type
ON events(task_id, event_type);

CREATE INDEX IF NOT EXISTS idx_events_created_at
ON events(created_at);

CREATE INDEX IF NOT EXISTS idx_loop_traces_task_loop
ON loop_traces(task_id, loop_id);

CREATE INDEX IF NOT EXISTS idx_loop_traces_task_committed
ON loop_traces(task_id, committed);

CREATE INDEX IF NOT EXISTS idx_stop_reports_task_loop
ON stop_reports(task_id, last_loop_id);

CREATE INDEX IF NOT EXISTS idx_accepted_checks_task
ON accepted_checks(task_id);

CREATE INDEX IF NOT EXISTS idx_accepted_checks_hunger_item
ON accepted_checks(task_id, hunger_item_id);

CREATE INDEX IF NOT EXISTS idx_hunger_items_task_status
ON hunger_items(task_id, status);

CREATE INDEX IF NOT EXISTS idx_evidence_task_loop
ON evidence(task_id, loop_id);

CREATE INDEX IF NOT EXISTS idx_worker_results_task_loop
ON worker_results(task_id, loop_id);

PRAGMA user_version = 4;
```

### 11.4 Required regression tests

```text
test_v3_migration_against_v2_db
test_v4_migration_against_v3_db
test_full_chain_v0_to_v4_against_fresh_db
```

The chain test is the load-bearing one — it catches column collisions
(c0-01's v2 already added `expires_at`; later migrations must not
re-add it).

---

## 12. CLI v0.5d

### 12.1 status

```bash
hungerloop status <task_id>
```

Must use protocol methods only.

Output:

```text
task_id
task status
last stop reason
phase
drive_budget
active_hunger
work_pressure
best_state_id
accepted checks count
open hunger items
blocked hunger items
total loops
total tokens
total cost
next actionable command
```

### 12.2 report

```bash
hungerloop report <task_id>
hungerloop report <task_id> --json
hungerloop report <task_id> --markdown
```

Report sections:

```text
Summary
Stop Reason
Best State
Hunger Status
Accepted Checks
Cost & Usage
Loop Timeline
Failures
Memory Candidates
Skill Cards
Recommended Next Actions
```

### 12.3 trace

```bash
hungerloop trace <task_id>
hungerloop trace <task_id> --loop 3
hungerloop trace export <task_id> --format jsonl --output trace.jsonl
```

Export behavior:

```text
By default, per-task export includes only events where task_id == requested task_id.
Global events are excluded unless --include-global is passed.
```

This resolves the review concern about ambiguous “global-event inclusion.”

### 12.4 repair-state

Existing D1-D7 checks are preserved.

v0.5d.1 adds:

```text
D8: accepted_checks reference missing validation_id
D9: best_state.validation_id missing
D10: final StopReport missing for stopped task
D11: usage snapshot missing or obviously inconsistent
D12: candidate workspace exists but candidate marked rejected/committed inconsistently
D13: event payload references missing loop_trace
```

Commands:

```bash
hungerloop repair-state <task_id>
hungerloop repair-state <task_id> --apply
```

`--apply` may only perform safe fixes:

```text
rebuild usage snapshot
create missing stop report from latest trace
mark orphan candidate as rejected
rebuild workspace manifest
```

It must not:

```text
auto-commit candidate
auto-promote memory
change accepted_check_keys
delete evidence
```

### 12.5 hunger unblock

```bash
hungerloop hunger unblock <task_id> <item_id>
hungerloop hunger unblock-all <task_id>
```

Behavior:

```text
status BLOCKED -> OPEN
consecutive_failure_count = 0
append shipped HUMAN_UNBLOCKED_HUNGER_ITEM event
```

Use the shipped event name, not a new renamed equivalent.

---

## 13. Resume preflight

The v0.5d PRD keeps the recovery matrix, but implements it as a CLI preflight layer before Orchestrator.

| Last StopReason | Default `run` | Required action |
|---|---|---|
| DONE | reject | `--reset` |
| HUNGER_EXPIRED | reject | refill, then `--resume` |
| BLOCKED | reject | unblock item(s), then `--resume` |
| HUMAN_REQUIRED | reject | resolve requirement, then `--resume` |
| HUMAN_PAUSED | reject | `hunger resume` or `--resume` |
| SAFETY_STOP | reject | raise ceiling, then `--resume` |
| ERROR | reject | run `repair-state`, then `--resume` or `--reset` |

The CLI must print actionable errors.

### 13.1 ERROR-recovery gating signal

The ERROR row's "inspect/repair, then resume" is otherwise unmeasurable
— `run` cannot tell whether the operator actually inspected anything.
Pin the signal:

```text
After an ERROR stop_report, ``run --resume`` proceeds ONLY if there
exists a ``repair_state_action`` event with:

  task_id     = <task_id>
  created_at  > stop_report.created_at

In other words, repair-state must have run AT LEAST ONCE since the
ERROR was recorded.
```

`run` looks up the latest `repair_state_action` event for the task via
`repo.list_events(task_id, event_types=["repair_state_action"])`,
takes the most recent, compares timestamps. If the check fails:

```text
Cannot resume task <task_id>: last stop reason is ERROR, and no
repair-state run has been recorded since.
Run:
  hungerloop repair-state <task_id>
then:
  hungerloop run <task_id> --resume

If repair-state surfaced no fixable divergences, pass --resume --skip-repair-check
to override (audit-logged via repair_state_action with action="skipped").
```

`--skip-repair-check` is the operator's escape hatch when the error
has nothing to do with on-disk state (e.g., a transient network blip
during a model call). The override writes a `repair_state_action`
event with `payload={"action": "skipped", "reason": "operator override"}`
so the audit trail still has a row to point at.

---

# Part II — v0.5e Memory & Skill Lifecycle

## 14. MemoryCandidate schema reconciliation

### 14.1 Hard rule

Do not rename shipped fields.

Keep:

```text
candidate_id
state
```

Do not introduce:

```text
id
status
```

### 14.2 Shipped-compatible lifecycle

Use existing `state` field. The type alias keeps the shipped name
`MemoryState` (declared in `models/memory.py` by c0-01); v0.5e only
extends the literal.

```python
MemoryState = Literal[
    "proposed",      # shipped in c0-01
    "approved",      # shipped in c0-01
    "rejected",      # shipped in c0-01
    "expired",       # shipped in c0-01
    "superseded",    # shipped in c0-01
    "deferred",      # NEW in v0.5e
]
```

Semantics:

```text
proposed   = pending review (default at emit time)
approved   = human-approved into PromotedMemory
rejected   = reviewed and rejected
deferred   = review postponed (NEW)
expired    = lifecycle sweep retired the row (v0.6 actor)
superseded = replaced by a newer candidate
```

Adding `deferred` is purely additive: no shipped row uses that value,
so existing pydantic validation passes unchanged.

### 14.3 Schema additions

Add fields only if missing:

```python
class MemoryCandidate(BaseModel):
    candidate_id: str
    task_id: str

    # Existing fields remain (shipped in v0.5a + c0-01).
    state: MemoryState = "proposed"
    referenced_check_keys: list[str] = Field(default_factory=list)
    source_loop_ids: list[int] = Field(default_factory=list)

    # New additive fields (v0.5e):
    source_candidate_state_id: str | None = None
    source_best_state_id: str | None = None
    source_validation_id: str | None = None

    accepted_check_keys: list[str] = Field(default_factory=list)

    action_verified: bool = False
    reusable: bool = False
    non_volatile: bool = False
    traceable: bool = False

    reviewer: str | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None
    # ``expires_at`` already exists from c0-01; do NOT redeclare.
```

Use `expires_at: datetime | None`, not `expires_when: str | None`, to
align with the shipped (c0-01) `expires_at` field.

### 14.4 `referenced_check_keys` vs `accepted_check_keys`

Both fields exist on purpose; the semantic split must be enforced by
`MemoryManager`:

```text
referenced_check_keys
    Every check_key the candidate's source loop touched, regardless
    of pass/fail. Already shipped; do NOT rename or migrate away.
    Used for "which check keys does this memory speak to?"

accepted_check_keys (NEW in v0.5e)
    Strict subset of referenced_check_keys: only the keys that
    *passed* in the source validation. Used for the
    ``action_verified`` predicate and the approval gate (a candidate
    cannot be approved if accepted_check_keys is empty).
```

The c0-01 shipped manager populates `referenced_check_keys` from
`validation.newly_passed_check_keys`. v0.5e changes the assignment:

```python
# v0.5e MemoryManager.propose_from_loop:
candidate = MemoryCandidate(
    ...,
    # Every key the source loop touched (existing semantics):
    referenced_check_keys=validation.attempted_check_keys
        or validation.newly_passed_check_keys,
    # Strict subset: only the ones that passed in this loop:
    accepted_check_keys=list(validation.newly_passed_check_keys),
)
```

This is forward-compatible: rows written by c0-01's manager will have
`accepted_check_keys=[]` and the predicate gate (§16) will treat them
as "ineligible until a re-evaluation populates the new field."

### 14.5 `MemoryType` reconciliation

Shipped `MemoryType = Literal["fact", "procedure", "preference",
"pitfall"]`. v0.5e additively expands the vocabulary; existing
single-token values remain valid.

```python
MemoryType = Literal[
    # Shipped — do NOT rename:
    "fact",
    "procedure",
    "preference",
    "pitfall",
    # v0.5e additions:
    "failure_pattern",
    "tool_usage",
    "workflow_pattern",
]
```

`procedure` is **not** renamed to `procedural_rule`; it's already
persisted as `procedure` in c0-01-shipped rows. New candidates may use
the additions; existing rows continue to validate.

---

## 15. Memory predicates

### 15.1 action_verified

```python
action_verified = bool(
    set(candidate.evidence_ids) & set(best_state.evidence_ids)
)
```

### 15.2 traceable

```python
traceable = set(candidate.evidence_ids).issubset(set(best_state.evidence_ids))
```

### 15.3 reusable

Do not use naive substring blacklist.

Use anchored regex patterns:

```python
TASK_SPECIFIC_PATTERNS = [
    r"\btask_[0-9a-fA-F-]+\b",
    r"\bloop_\d{3}\b",
    r"^/tmp/",
    r"workspace/tasks/[a-zA-Z0-9_-]+",
    r"\bCAND-[a-zA-Z0-9_-]+\b",
    r"\bVAL-[a-zA-Z0-9_-]+\b",
]
```

Then:

```python
def is_reusable(content: str) -> bool:
    return not any(re.search(pattern, content) for pattern in TASK_SPECIFIC_PATTERNS)
```

The test corpus must include at least 10 real HungerLoop outputs to avoid false positives.

### 15.4 non_volatile

v0.5e uses weak non-volatile:

```python
non_volatile = (
    candidate.source_best_state_id == stop_report.final_best_state_id
    and stop_report.stop_reason == StopReason.DONE
)
```

If `source_best_state_id` is absent, compute it from:

```text
candidate.source_candidate_state_id -> candidate -> best state relationship
```

Strong non-volatile is v0.6:

```text
The memory remains true across >=2 committed best states.
```

---

## 16. Memory lifecycle CLI

```bash
hungerloop memory list <task_id>
hungerloop memory show <candidate_id>
hungerloop memory approve <candidate_id>
hungerloop memory reject <candidate_id> --reason "too task-specific"
hungerloop memory defer <candidate_id>
hungerloop memory expire <candidate_id>
hungerloop memory promoted list
```

Approval rules:

```text
candidate.state in {"proposed", "deferred"}
candidate.evidence_ids non-empty
candidate.action_verified == True
candidate.traceable == True
```

If reusable is false:

```bash
hungerloop memory approve <candidate_id> --force
```

---

## 17. PromotedMemory

```python
class PromotedMemory(BaseModel):
    memory_id: str
    source_candidate_id: str

    content: str
    memory_type: str
    layer: str

    evidence_ids: list[str]
    accepted_check_keys: list[str]
    reuse_scenarios: list[str]

    confidence: float
    created_at: datetime
    approved_by: str
```

No automatic promotion in v0.5e.

---

## 18. SkillCardCandidate

### 18.1 Trigger rule

Create SkillCardCandidate only when:

```text
1. stop_report.stop_reason == DONE
2. final BestState exists
3. len(best.accepted_check_keys) >= 2
4. at least one successful tool_call evidence exists
5. final committed LoopTrace has no regressed_check_keys
```

### 18.2 Required Repository method

Add to Protocol if not present:

```python
def list_successful_tool_call_evidence(self, task_id: str) -> list[Evidence]: ...
```

### 18.3 Schema

```python
class SkillCardCandidate(BaseModel):
    skill_candidate_id: str
    task_id: str
    source_best_state_id: str
    source_stop_report_id: str | None = None

    name: str
    description: str

    trigger_signals: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    tools_used: list[str] = Field(default_factory=list)
    accepted_check_keys: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    known_failures: list[str] = Field(default_factory=list)
    reuse_notes: list[str] = Field(default_factory=list)

    state: Literal["candidate", "active", "rejected", "deprecated"] = "candidate"
    created_at: datetime
```

### 18.4 Deterministic derivation

No LLM calls in skill generation. Each helper has a pinned algorithm
so two implementations cannot diverge silently.

```python
derive_name(best_state) -> str
derive_trigger_signals(best_state, traces) -> list[str]
derive_preconditions(best_state, traces) -> list[str]
derive_steps(traces) -> list[str]
derive_tools(traces) -> list[str]
derive_failures(task_id, repo) -> list[str]
```

Algorithms (all pure functions; no IO except `derive_failures` which
reads evidence rows):

```python
def derive_name(best_state) -> str:
    """First non-empty line of best_state.summary, truncated to 80
    chars and stripped of trailing punctuation. Empty summary →
    'Skill from task <task_id>'."""
    head = next(
        (line.strip() for line in best_state.summary.splitlines() if line.strip()),
        f"Skill from task {best_state.task_id}",
    )
    return head[:80].rstrip(".:;,!?")


def derive_steps(traces) -> list[str]:
    """One step per committed LoopTrace, in chronological order.
    Step text is `delta_summary` if non-empty, else
    f'Loop {loop_id}: committed candidate {candidate_state_id}'.
    Rejected loops are skipped — we only persist the success path."""
    steps: list[str] = []
    for t in sorted((t for t in traces if t.committed), key=lambda x: x.loop_id):
        text = t.delta_summary.strip() or (
            f"Loop {t.loop_id}: committed candidate {t.candidate_state_id}"
        )
        steps.append(text)
    return steps


def derive_tools(traces, repo) -> list[str]:
    """Unique tool names from `tool_call_succeeded` evidence rows
    referenced by committed traces, sorted by first-seen loop_id then
    name. De-dupe is case-sensitive (matches what tool_harness
    registered)."""
    seen: dict[str, int] = {}  # tool_name -> first loop_id
    for t in sorted(traces, key=lambda x: x.loop_id):
        if not t.committed:
            continue
        for ev in repo.list_successful_tool_call_evidence(t.task_id):
            if ev.loop_id != t.loop_id:
                continue
            name = ev.payload.get("tool_name")
            if isinstance(name, str) and name not in seen:
                seen[name] = t.loop_id
    return sorted(seen.keys(), key=lambda n: (seen[n], n))


def derive_trigger_signals(best_state, traces) -> list[str]:
    """Accepted check types (parsed from check_keys via the
    AcceptanceCheckType registry) plus artifact_type values from
    best_state.artifact_ids. Sorted, de-duped."""
    out: set[str] = set()
    for key in best_state.accepted_check_keys:
        # Format: '<item_id>:<index>'; lookup check type via repo.
        out.add(f"check:{key.split(':', 1)[0]}")
    for art_id in best_state.artifact_ids:
        # Artifact rows carry artifact_type column; SQL query.
        out.add(f"artifact:{art_id}")
    return sorted(out)


def derive_preconditions(best_state, traces) -> list[str]:
    """Static rule set — same for every candidate in v0.5e:
      1. 'workspace clean (no uncommitted candidates)'
      2. 'previously-passed checks: ' + count of best.accepted_check_keys
    The list is intentionally short; v0.6 may LLM-generate richer ones."""
    return [
        "workspace clean (no uncommitted candidates)",
        f"previously-passed checks: {len(best_state.accepted_check_keys)}",
    ]


def derive_failures(task_id, repo) -> list[str]:
    """One entry per `tool_call_failed` and `model_call_failed` event
    in the task. Format: f'{event_type}: {payload.get(\"error_type\",
    \"unknown\")} (loop {loop_id})'. Capped at 20 entries — older
    failures are dropped, newest kept."""
    rows = repo.list_events(
        task_id,
        event_types=["tool_call_failed", "model_call_failed"],
    )
    formatted = [
        f"{e.event_type}: {e.payload.get('error_type', 'unknown')} (loop {e.loop_id})"
        for e in rows
    ]
    return formatted[-20:]
```

### 18.5 Required golden-output test

```text
test_skill_card_derivation_deterministic
```

Build a fixture task with three committed loops, two rejected loops,
five tool_call_succeeded evidence rows (three distinct tools), and one
tool_call_failed event. Assert the produced SkillCardCandidate has
exactly:

```text
steps == ["delta of loop 1", "delta of loop 3", "delta of loop 5"]
tools_used == ["tool_a", "tool_b", "tool_c"]   # sorted by first-seen
known_failures == ["tool_call_failed: timeout (loop 4)"]
preconditions has length 2
trigger_signals is sorted
```

Re-running the same fixture twice must produce byte-identical output —
that's the determinism guarantee.

---

## 19. Skill CLI

```bash
hungerloop skill list
hungerloop skill show <skill_candidate_id>
hungerloop skill approve <skill_candidate_id>
hungerloop skill reject <skill_candidate_id> --reason "not reusable"
hungerloop skill export <skill_id> --output skill.yaml
hungerloop skill import skill.yaml
```

Approval:

```text
candidate state must be candidate
human approval required
no automatic activation
```

---

## 20. SQLite migrations for v0.5e

### 20.1 Migration file

```text
migrations/v5__memory_skill_lifecycle_extensions.sql
```

Bump:

```python
SQLiteMigrator.LATEST_VERSION = 5
```

(v0.5d ships v3 + v4; v0.5e ships v5. See §11.)

### 20.2 Columns already shipped by v2 — DO NOT re-add

c0-01's `migrations/v2__memory_candidate_lifecycle.sql` already added:

```text
state                   TEXT NOT NULL DEFAULT 'proposed'
decision_loop_id        INTEGER
decided_by              TEXT
decision_rationale      TEXT NOT NULL DEFAULT ''
replaces_candidate_id   TEXT
expires_at              TEXT
idx_memory_state(task_id, state)
```

SQLite's `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` clause —
re-adding any of those columns in v5 will throw `duplicate column
name` and the whole migration rolls back. v5 must skip them.

### 20.3 v5 — predicates + provenance + review fields

`migrations/v5__memory_skill_lifecycle_extensions.sql`:

```sql
-- Memory provenance / predicates / review (none of these are in v2).
ALTER TABLE memory_candidates ADD COLUMN source_candidate_state_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN source_best_state_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN source_validation_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN accepted_check_keys_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_candidates ADD COLUMN action_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN reusable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN non_volatile INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN traceable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN reviewer TEXT;
ALTER TABLE memory_candidates ADD COLUMN reviewed_at TEXT;
ALTER TABLE memory_candidates ADD COLUMN rejection_reason TEXT;
-- expires_at intentionally omitted; already in v2.

-- PromotedMemory store (new in v0.5e).
CREATE TABLE IF NOT EXISTS promoted_memories (
  memory_id TEXT PRIMARY KEY,
  source_candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id),
  content TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  layer TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  accepted_check_keys_json TEXT NOT NULL,
  reuse_scenarios_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

PRAGMA user_version = 5;
```

The skill-card tables follow in §20.4.

### 20.4 Skill tables (new — no shipped equivalent at the candidate tier)

The v0.5a `SkillCard` model (`models/skill.py`) is single-tier
(skill_id + name + steps + checks); it does **not** carry a candidate
lifecycle. v0.5e introduces the candidate / active split, which means
the new tables are net-additions, not migrations of the existing
`skill_cards` row format.

Append to `v5__memory_skill_lifecycle_extensions.sql`:

```sql
CREATE TABLE IF NOT EXISTS skill_card_candidates (
  skill_candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  source_best_state_id TEXT NOT NULL,
  source_stop_report_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  trigger_signals_json TEXT NOT NULL,
  preconditions_json TEXT NOT NULL,
  steps_json TEXT NOT NULL,
  tools_used_json TEXT NOT NULL,
  accepted_check_keys_json TEXT NOT NULL,
  artifact_ids_json TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  known_failures_json TEXT NOT NULL,
  reuse_notes_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_skill_cards (
  skill_id TEXT PRIMARY KEY,
  source_candidate_id TEXT NOT NULL REFERENCES skill_card_candidates(skill_candidate_id),
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  trigger_signals_json TEXT NOT NULL,
  preconditions_json TEXT NOT NULL,
  steps_json TEXT NOT NULL,
  tools_used_json TEXT NOT NULL,
  accepted_check_keys_json TEXT NOT NULL,
  artifact_ids_json TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  known_failures_json TEXT NOT NULL,
  reuse_notes_json TEXT NOT NULL,
  activated_at TEXT NOT NULL,
  activated_by TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
```

The shipped `skill_cards` table (if any) and `models/skill.py:SkillCard`
remain intact for v0.5a callers; v0.5e's CLI reads from
`active_skill_cards` exclusively.

### 20.5 Required regression test

```text
test_v5_migration_against_v4_db
test_v5_migration_idempotency_against_already_v2_db_with_expires_at
```

The second test drops a synthetic v2 DB with `expires_at` populated,
runs v5, and asserts no `duplicate column name` error.

---

## 21. v0.5d revised acceptance criteria

v0.5d is complete when:

```text
1. v0.5b.2 SQLiteRepository hard gate is done.
2. status/report/trace/repair-state do not access repo private fields.
3. RepositoryProtocol has all observability query methods and parity tests.
4. EventType additions are additive only; shipped names are preserved.
5. Orchestrator emits lifecycle events.
6. LoopTrace v2 fields are forward-compatible and persisted.
7. StopReport v2 fields are forward-compatible and persisted.
8. trace export behavior around global events is explicit and tested.
9. repair-state D8-D13 checks exist.
10. synthetic worker exception still persists trace and stop report.
```

---

## 22. v0.5e revised acceptance criteria

v0.5e is complete when:

```text
1. MemoryCandidate keeps candidate_id and state fields.
2. MemoryCandidate states support proposed/approved/rejected/deferred/expired/superseded.
3. Memory predicates are computed and persisted.
4. reusable predicate uses anchored regex, not naive substrings.
5. memory approve/reject/defer/expire CLI works.
6. PromotedMemory requires human approval.
7. SkillCardCandidate trigger rule is deterministic and tested.
8. list_successful_tool_call_evidence exists in Protocol and both repos.
9. SkillCardCandidate generation does not call LLM.
10. Skill export/import works.
```

---

## 23. Revised sequencing

### 23.1 v0.5b.2 — hard gate

```text
SQLiteRepository
CLI default SQLite backend
RepositoryProtocol parity tests
WAL + task lock persistence
```

### 23.2 v0.5d.0 — delta observability

```text
Protocol observability methods
Remove private repo reads
LoopTrace v2 additive fields
StopReport v2 additive fields
Additive EventType members
Orchestrator lifecycle events
Migration v3
```

### 23.3 v0.5d.1 — recovery hardening

```text
Resume preflight tests
repair-state D8-D13
repair-state --apply safe fixes
hunger unblock UX
trace export global event semantics
synthetic worker exception trace persistence
```

### 23.4 v0.5e.0 — memory reconciliation

```text
Schema reconciliation with shipped c0-01
MemoryCandidate predicates
memory CLI
PromotedMemory table
Migration v4 partial
```

### 23.5 v0.5e.1 — SkillCard lifecycle

```text
SkillCardCandidate
deterministic derivation
skill CLI
export/import
report integration
```

---

## 24. Non-goals

Still not included:

```text
3×3 Agent
LLMPlanner
LLM-as-judge
Automatic memory promotion
Vector search
Semantic skill retrieval
Background scheduler
Web UI
FastAPI
```

---

## 25. Final Definition of Done

v0.5d/e is done when HungerLoop can:

```text
1. Persist tasks through SQLiteRepository.
2. Explain every loop through LoopTrace and EventLog.
3. Explain every stop through StopReport.
4. Give a concrete recovery command for every StopReason.
5. Repair safe state inconsistencies without guessing.
6. Generate MemoryCandidate without breaking shipped schema.
7. Approve/reject/defer MemoryCandidate with audit trail.
8. Generate SkillCardCandidate only under deterministic trigger rules.
9. Export/import SkillCards.
10. Preserve all shipped wire contracts.
```

---

## 26. Developer warning

Do not implement the previous v0.5d/e PRD literally.

Specifically, do **not**:

```text
rename MemoryCandidate.candidate_id to id
rename MemoryCandidate.state to status
rename MemoryState type alias to MemoryCandidateState
rename MemoryCandidate.referenced_check_keys (keep both fields per §14.4)
rename MemoryType "procedure" to "procedural_rule"
replace shipped EventType names
re-add the ``expires_at`` column in v5 (already in v2)
ship a single migration that mixes schema additions and indexes
pretend SQLiteRepository is already shipped
read repo private fields from CLI
use naive substring reusable blacklist
create SkillCard without successful tool evidence
generate SkillCard with an LLM
hand-roll non-deterministic skill derivation (use the §18.4 algorithms)
auto-promote memory
allow ``run --resume`` after ERROR without a recent ``repair_state_action`` event
```

This revised PRD is the implementation source of truth.
