# HungerLoop Agent Harness — v0.4.1 Engineering Fix

**版本**：v0.4.1 engineering fix  
**日期**：2026-04-28  
**基线版本**：v0.4 Real MVP PRD  
**变更性质**：不扩大 MVP 范围，只修复 v0.4 在工程实现前必须解决的语义、验证、workspace、副作用和成本控制问题。  
**目标**：让 v0.4 从“方向正确的 MVP PRD”变成“可以直接进入实现的工程规格”。

---

## 0. 版本定位

v0.4.1 不是 v0.5，也不是功能扩展版。

它只做一件事：

> **保留 v0.4 的真实 MVP 收敛方向，但修掉会直接影响实现正确性的 P0 工程问题。**

v0.4 已经做对了这些判断：

```text
1. MVP 使用 binary ValidationGate，不做复杂打分。
2. MVP 使用显式 acceptance_checks，不用关键词匹配。
3. MVP 只做 CLI，不急着做 API。
4. MVP 只启用一个真实 worker，不急着做 3×3 Agent。
5. MVP 只生成 MemoryCandidate，不自动晋升长期记忆。
6. MVP 引入 CostTracker 和 StagnationDetector。
7. MVP 保留 V2 扩展接口，但不提前实现 V2。
```

v0.4.1 在这些判断之上修复：

```text
1. score monotonic invariant 与 binary gate 的冲突。
2. has_real_progress 粒度过粗。
3. rejected candidate 已经污染真实 workspace 的问题。
4. ValidationGate 每轮验证所有 open items 的误判问题。
5. BLOCKED 被排除后可能误判 DONE 的问题。
6. AcceptanceCheckRunner 绕过 ToolHarness / sandbox 的问题。
7. path 检查可能逃逸 workspace 的问题。
8. cost ceiling 只在 loop 开始检查的问题。
9. loop_count decay 的 off-by-one 语义问题。
10. Memory consolidation 默认阻塞 DONE 的问题。
```

---

## 1. v0.4.1 的核心结论

### 1.1 MVP 不使用 score 作为提交条件

v0.4 中存在语义冲突：

```text
一方面说 MVP 不打分，ValidationReport.score 永远 0.0。
另一方面 invariant I-3 仍然写着 score_after > score_before 才能提交。
```

v0.4.1 修正为：

```text
MVP commit 不依赖 score。
MVP commit 依赖 check-level progress。
score 字段保留给 V1.2 LLM-as-judge。
```

新的提交核心：

```text
Candidate 可提交，当且仅当：

1. verdict ∈ {PASS, PARTIAL}
2. newly_passed_check_keys 非空
3. regressed_check_keys 为空
4. missing_evidence 为空
5. candidate workspace 验证通过
```

---

### 1.2 MVP 单调性不是 score monotonic，而是 acceptance frontier monotonic

v0.4.1 用新的单调性定义替代旧的分数单调性。

```text
旧定义：
BestState.score 必须越来越高。

新定义：
BestState 已经通过的 acceptance check 集合不能回退。
```

也就是：

```text
AcceptedFrontier_t ⊆ AcceptedFrontier_t+1
```

其中：

```text
AcceptedFrontier = set[check_key]
check_key = "{hunger_item_id}:{check_index}"
```

例如：

```text
H-001:0 = report.md 文件存在
H-001:1 = pytest 通过
H-002:0 = 至少 1 条 evidence
```

如果某轮新通过了 `H-001:0`，即使整个 H-001 还没全部满足，也算真实进展，可以 commit。

---

### 1.3 Candidate 必须在隔离 workspace 中执行

v0.4 只保证数据库里的 BestState 不被未验证 candidate 覆盖。  
但如果 Worker 已经直接写真实 workspace，那么 rejected candidate 仍然污染了文件系统。

v0.4.1 强制引入：

```text
best workspace
candidate workspace
workspace promotion
workspace archive
```

每轮流程：

```text
best/files
  ↓ copy
candidates/loop_x/files
  ↓ worker writes here
validation checks candidate workspace
  ↓ if pass
promote candidate workspace to best
  ↓ if fail
archive candidate workspace, best unchanged
```

这才是真正的 commit / reject。

---

### 1.4 ValidationGate 只验证本轮 target items + regression items

v0.4 每轮验证所有 open hunger items。  
这会导致没有被本轮尝试的 item 也被算作失败，并错误触发 stagnation。

v0.4.1 改为：

```text
target checks：
  本轮 LoopPlan.selected_hunger_item_ids

regression checks：
  之前已经通过的 check_keys
```

ValidationGate 不再对所有 open items 做失败计数。

---

### 1.5 BLOCKED 是停止状态，不是 DONE

v0.4 中 `work_pressure()` 排除了 BLOCKED item。  
如果所有剩余 item 都 blocked，work_pressure 会变成 0，从而可能误判 DONE。

v0.4.1 明确：

```text
所有剩余 item 都 BLOCKED → StopReason.BLOCKED
所有 item CLOSED / VALIDATED_SATISFIED → StopReason.DONE
drive_budget 归零但仍有 active/blocking item → HUNGER_EXPIRED
```

---

## 2. v0.4.1 新 invariant

v0.4.1 的系统不变量如下。

```text
I-1: drive_budget 只能由 HungerClock 衰减或人类操作改变。

I-2: hunger_item.gap_score 只能由 ValidationReport 或人类操作降低。

I-3: MVP 不使用 score 作为 commit 条件。
     BestState 的单调性由 accepted_check_keys 保证。

I-4: Candidate 必须在 candidate workspace 中执行。
     Rejected candidate 不能污染 best workspace。

I-5: ValidationGate 只验证 target_hunger_item_ids + previously_passed_check_keys。
     不得把未尝试的 open item 当成本轮失败。

I-6: StagnationDetector 只对 attempted target items 增加失败计数。

I-7: AcceptanceCheckRunner 不得直接执行 shell 或读写任意 path。
     必须通过 SandboxRunner / ToolHarness 的统一安全层。

I-8: cost ceiling 不只在 loop 开始检查。
     LLM/tool 调用前后都必须检查。

I-9: BLOCKED 与 DONE 严格区分。
     blocked item 不参与 active work_pressure，但会影响 stop_reason。

I-10: Memory consolidation 默认不阻塞 DONE。
      只有人类显式启用时才作为 active HungerItem。
```

---

## 3. 变更总览

| 模块 | v0.4 | v0.4.1 |
|---|---|---|
| Commit invariant | `score_after > score_before` | `newly_passed_check_keys 非空` |
| Progress 粒度 | item-level | check-level |
| BestState 单调性 | score monotonic | acceptance frontier monotonic |
| Workspace | Worker 直接写 workspace | candidate workspace 隔离 |
| Validation 范围 | 所有 open items | target items + regression checks |
| Stagnation 范围 | 所有 unsatisfied items | attempted target items |
| BLOCKED 语义 | 可能被排除后 DONE | 显式 StopReason.BLOCKED |
| Shell check | `create_subprocess_shell` | `SandboxRunner.run_argv()` |
| Path check | 简单拼接 | `resolve_workspace_path()` |
| Cost ceiling | loop 开始检查 | 调用前后检查 |
| Memory item | 默认 H-003 且 human approval | 默认不 active，可选启用 |
| Loop count decay | 可能 off-by-one | 明确定义已完成 loop 数 |

---

## 4. 数据模型变更

### 4.1 CheckKey

v0.4.1 引入 check-level 状态。

```python
CheckKey = str  # "{hunger_item_id}:{check_index}"
```

示例：

```python
"H-001:0"
"H-001:1"
"H-002:0"
```

工具函数：

```python
def make_check_key(hunger_item_id: str, check_index: int) -> str:
    return f"{hunger_item_id}:{check_index}"
```

---

### 4.2 CheckResult

v0.4 的 `CheckResult` 只记录某个 check 是否通过。  
v0.4.1 增加：

```text
check_key
previously_passed
newly_passed
regressed
workspace_ref
```

代码：

```python
# src/hungerloop/models/validation.py
from pydantic import BaseModel


class CheckResult(BaseModel):
    hunger_item_id: str
    check_index: int
    check_key: str
    check_type: str

    passed: bool
    previously_passed: bool = False
    newly_passed: bool = False
    regressed: bool = False

    detail: str
    evidence_id: str | None = None
    workspace_ref: str | None = None
```

---

### 4.3 ValidationReport

```python
# src/hungerloop/models/validation.py
from pydantic import BaseModel, Field
from hungerloop.models.enums import ValidationVerdict


class ValidationReport(BaseModel):
    id: str
    task_id: str
    loop_id: int
    candidate_state_id: str
    baseline_state_id: str | None

    verdict: ValidationVerdict

    # MVP: 保留字段，但不作为 commit 条件。
    score_before: float = 0.0
    score_after: float = 0.0
    score_delta: float = 0.0

    attempted_hunger_item_ids: list[str] = Field(default_factory=list)

    check_results: list[CheckResult] = Field(default_factory=list)

    currently_passed_check_keys: list[str] = Field(default_factory=list)
    newly_passed_check_keys: list[str] = Field(default_factory=list)
    regressed_check_keys: list[str] = Field(default_factory=list)

    satisfied_hunger_item_ids: list[str] = Field(default_factory=list)
    unsatisfied_hunger_item_ids: list[str] = Field(default_factory=list)

    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)

    recommended_next_actions: list[str] = Field(default_factory=list)

    has_real_progress: bool = False
```

---

### 4.4 BestState

BestState 增加 `accepted_check_keys`，表示当前已提交状态通过了哪些 acceptance checks。

```python
# src/hungerloop/models/blackboard.py
from pydantic import BaseModel, Field


class BestState(BaseModel):
    task_id: str
    state_id: str
    summary: str

    # V1.2 LLM-as-judge 使用，MVP 不作为 commit 条件。
    score: float = 0.0

    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    validation_id: str | None = None
    updated_at_loop: int = 0

    # NEW
    accepted_check_keys: list[str] = Field(default_factory=list)
    workspace_ref: str = "best"
```

---

### 4.5 CandidateState

CandidateState 增加 workspace_ref。

```python
class CandidateState(BaseModel):
    id: str
    task_id: str
    loop_id: int
    summary: str

    artifact_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    proposed_score: float = 0.0

    # NEW
    workspace_ref: str  # e.g. "candidates/loop_003"
```

---

### 4.6 WorkspaceManifest

```python
# src/hungerloop/models/workspace.py
from pydantic import BaseModel, Field


class WorkspaceManifest(BaseModel):
    task_id: str
    loop_id: int | None = None
    workspace_ref: str
    path: str

    source_workspace_ref: str | None = None
    status: str = "candidate"  # candidate | best | archived | rejected

    created_by: str
    created_at: str

    file_count: int = 0
    total_bytes: int = 0
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
```

---

## 5. 枚举变更

### 5.1 StopReason 增加 HUMAN_REQUIRED

`BLOCKED` 表示系统无法继续推进。  
`HUMAN_REQUIRED` 表示系统知道下一步需要人类输入、授权或确认。

```python
class StopReason(str, Enum):
    DONE = "done"
    HUNGER_EXPIRED = "hunger_expired"
    BLOCKED = "blocked"
    HUMAN_REQUIRED = "human_required"  # NEW
    HUMAN_PAUSED = "human_paused"
    SAFETY_STOP = "safety_stop"
    ERROR = "error"
```

### 5.2 AcceptanceCheckType 建议调整

v0.4 中 `SHELL_EXIT_ZERO` 用字符串 shell。  
v0.4.1 建议保留名字，但 params 必须使用 argv。

```python
class AcceptanceCheckType(str, Enum):
    FILE_EXISTS = "file_exists"
    SHELL_EXIT_ZERO = "shell_exit_zero"
    EVIDENCE_COUNT_MIN = "evidence_count_min"
    ARTIFACT_TYPE_EXISTS = "artifact_type_exists"
    HUMAN_APPROVAL = "human_approval"
    LLM_JUDGE = "llm_judge"
```

新参数格式：

```json
{
  "check_type": "shell_exit_zero",
  "params": {
    "argv": ["pytest", "tests/test_foo.py"],
    "timeout": 60
  }
}
```

兼容旧参数：

```json
{
  "cmd": "pytest tests/test_foo.py"
}
```

MVP 可以支持 `cmd`，但必须转换为 argv 或明确拒绝：

```python
if "cmd" in params:
    raise ValueError("Use argv instead of shell cmd in MVP.")
```

---

## 6. Workspace 隔离设计

### 6.1 目录结构

```text
workspace/
  tasks/
    task_001/
      best/
        files/
        manifest.json

      candidates/
        loop_001/
          files/
          manifest.json
        loop_002/
          files/
          manifest.json

      rejected/
        loop_001/
          files/
          manifest.json

      evidence/
      artifacts/
      blackboard.sqlite
      events.jsonl
```

### 6.2 规则

```text
1. Worker 只能写 candidate workspace。
2. Validation 只能检查 candidate workspace。
3. Commit 成功后，candidate workspace 被 promote 为 best。
4. Commit 失败后，candidate workspace 被 archive/rejected。
5. best workspace 不能被 Worker 直接写入。
```

---

### 6.3 WorkspaceManager

```python
# src/hungerloop/services/workspace_manager.py
from pathlib import Path
import shutil
import json
from datetime import datetime, timezone


class WorkspaceManager:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def task_root(self, task_id: str) -> Path:
        return self.root / "tasks" / task_id

    def best_files_dir(self, task_id: str) -> Path:
        return self.task_root(task_id) / "best" / "files"

    def candidate_files_dir(self, task_id: str, loop_id: int) -> Path:
        return self.task_root(task_id) / "candidates" / f"loop_{loop_id:03d}" / "files"

    def rejected_files_dir(self, task_id: str, loop_id: int) -> Path:
        return self.task_root(task_id) / "rejected" / f"loop_{loop_id:03d}" / "files"

    def ensure_task_workspace(self, task_id: str) -> None:
        best = self.best_files_dir(task_id)
        best.mkdir(parents=True, exist_ok=True)

    def create_candidate_workspace(self, task_id: str, loop_id: int) -> Path:
        self.ensure_task_workspace(task_id)

        src = self.best_files_dir(task_id)
        dst = self.candidate_files_dir(task_id, loop_id)

        if dst.exists():
            shutil.rmtree(dst)

        if src.exists():
            shutil.copytree(src, dst)
        else:
            dst.mkdir(parents=True, exist_ok=True)

        self._write_manifest(
            task_id=task_id,
            loop_id=loop_id,
            path=dst,
            status="candidate",
            source_workspace_ref="best",
        )
        return dst

    def promote_candidate_to_best(self, task_id: str, loop_id: int) -> None:
        candidate = self.candidate_files_dir(task_id, loop_id)
        best = self.best_files_dir(task_id)

        if not candidate.exists():
            raise FileNotFoundError(f"Candidate workspace not found: {candidate}")

        backup = self.task_root(task_id) / "best_backup"
        if backup.exists():
            shutil.rmtree(backup)

        if best.exists():
            shutil.move(str(best), str(backup))

        shutil.copytree(candidate, best)

        if backup.exists():
            shutil.rmtree(backup)

        self._write_manifest(
            task_id=task_id,
            loop_id=None,
            path=best,
            status="best",
            source_workspace_ref=f"candidates/loop_{loop_id:03d}",
        )

    def reject_candidate(self, task_id: str, loop_id: int) -> None:
        candidate = self.candidate_files_dir(task_id, loop_id)
        rejected = self.rejected_files_dir(task_id, loop_id)

        if not candidate.exists():
            return

        if rejected.exists():
            shutil.rmtree(rejected)

        rejected.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), str(rejected))

        self._write_manifest(
            task_id=task_id,
            loop_id=loop_id,
            path=rejected,
            status="rejected",
            source_workspace_ref=f"candidates/loop_{loop_id:03d}",
        )

    def _write_manifest(
        self,
        task_id: str,
        loop_id: int | None,
        path: Path,
        status: str,
        source_workspace_ref: str | None,
    ) -> None:
        files = [p for p in path.rglob("*") if p.is_file()]
        manifest = {
            "task_id": task_id,
            "loop_id": loop_id,
            "workspace_ref": self.workspace_ref_for_path(path),
            "path": str(path),
            "source_workspace_ref": source_workspace_ref,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "total_bytes": sum(p.stat().st_size for p in files),
        }
        (path.parent / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def workspace_ref_for_path(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.task_root_from_path(path))
        return str(rel)

    def task_root_from_path(self, path: Path) -> Path:
        resolved = path.resolve()
        parts = resolved.parts
        if "tasks" not in parts:
            raise ValueError("Path is not inside tasks workspace.")
        idx = parts.index("tasks")
        return Path(*parts[: idx + 2])
```

---

## 7. Path 安全层

### 7.1 统一路径解析

任何工具、acceptance check、artifact 注册都必须使用同一套路径安全函数。

```python
# src/hungerloop/services/path_safety.py
from pathlib import Path


def resolve_workspace_path(workspace_root: Path, user_path: str) -> Path:
    root = workspace_root.resolve()

    # 禁止空路径
    if not user_path or user_path.strip() == "":
        raise ValueError("Empty path is not allowed.")

    # 禁止绝对路径输入
    raw = Path(user_path)
    if raw.is_absolute():
        raise PermissionError(f"Absolute path is not allowed: {user_path}")

    resolved = (root / raw).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(f"Path escapes workspace: {user_path}")

    return resolved
```

### 7.2 用法

```python
candidate_root = workspace_manager.candidate_files_dir(task_id, loop_id)
path = resolve_workspace_path(candidate_root, check.params["path"])
```

不要写：

```python
path = workspace_root / check.params["path"]
```

---

## 8. SandboxRunner

### 8.1 设计思路

Validation 的 shell check 不能直接调用 `asyncio.create_subprocess_shell`。  
必须经过统一 sandbox：

```text
SandboxRunner
  - 禁止 shell=True
  - argv 数组执行
  - cwd 必须在 candidate workspace 内
  - timeout 必填
  - stdout/stderr 截断
  - evidence 自动保存
  - cost/tool event 自动记录
```

### 8.2 代码

```python
# src/hungerloop/services/sandbox_runner.py
import asyncio
from pathlib import Path
from pydantic import BaseModel


class SandboxRunResult(BaseModel):
    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    evidence_id: str | None = None


class SandboxRunner:
    def __init__(self, repo, max_output_chars: int = 5000):
        self.repo = repo
        self.max_output_chars = max_output_chars

    async def run_argv(
        self,
        task_id: str,
        loop_id: int,
        argv: list[str],
        cwd: Path,
        timeout: int,
        evidence_label: str,
    ) -> SandboxRunResult:
        if not argv:
            raise ValueError("argv cannot be empty")

        if timeout <= 0:
            raise ValueError("timeout must be positive")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            timed_out = True

        stdout = stdout_b.decode(errors="replace")[: self.max_output_chars]
        stderr = stderr_b.decode(errors="replace")[: self.max_output_chars]
        exit_code = proc.returncode if proc.returncode is not None else -1

        evidence_id = self.repo.save_shell_output_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            label=evidence_label,
            argv=argv,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

        return SandboxRunResult(
            argv=argv,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            evidence_id=evidence_id,
        )
```

---

## 9. AcceptanceCheckRunner v0.4.1

### 9.1 设计变化

v0.4 的 AcceptanceCheckRunner：

```text
直接读 workspace_root
直接 create_subprocess_shell
```

v0.4.1 改为：

```text
只读 candidate workspace
使用 resolve_workspace_path
shell check 走 SandboxRunner.run_argv
```

### 9.2 代码

```python
# src/hungerloop/services/acceptance_runner.py
from pathlib import Path
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.hunger import AcceptanceCheck
from hungerloop.services.path_safety import resolve_workspace_path


class AcceptanceCheckRunner:
    def __init__(self, repo, workspace_manager, sandbox_runner):
        self.repo = repo
        self.workspace_manager = workspace_manager
        self.sandbox_runner = sandbox_runner

    async def run(
        self,
        check: AcceptanceCheck,
        task_id: str,
        loop_id: int,
        candidate,
    ) -> tuple[bool, str, str | None]:
        ct = check.check_type
        candidate_root = self.workspace_manager.candidate_files_dir(task_id, loop_id)

        if ct == AcceptanceCheckType.FILE_EXISTS:
            path = resolve_workspace_path(candidate_root, check.params["path"])
            ok = path.exists() and path.is_file()
            return ok, f"file_exists({check.params['path']}): {ok}", None

        if ct == AcceptanceCheckType.SHELL_EXIT_ZERO:
            if "argv" not in check.params:
                raise ValueError("SHELL_EXIT_ZERO requires params.argv in MVP.")

            argv = check.params["argv"]
            timeout = int(check.params.get("timeout", 60))

            result = await self.sandbox_runner.run_argv(
                task_id=task_id,
                loop_id=loop_id,
                argv=argv,
                cwd=candidate_root,
                timeout=timeout,
                evidence_label=f"acceptance:{candidate.id}",
            )

            ok = result.exit_code == 0 and not result.timed_out
            detail = f"shell_exit_zero(argv={argv}): exit={result.exit_code}, timeout={result.timed_out}"
            return ok, detail, result.evidence_id

        if ct == AcceptanceCheckType.EVIDENCE_COUNT_MIN:
            ev_type = check.params.get("evidence_type", "any")
            min_count = int(check.params["min_count"])
            count = self.repo.count_evidence_by_type(
                task_id=task_id,
                evidence_ids=candidate.evidence_ids,
                evidence_type=ev_type,
            )
            ok = count >= min_count
            return ok, f"evidence_count({ev_type}): {count}/{min_count}", None

        if ct == AcceptanceCheckType.ARTIFACT_TYPE_EXISTS:
            art_type = check.params["artifact_type"]
            artifacts = self.repo.get_artifacts_by_ids(candidate.artifact_ids)
            ok = any(a.artifact_type == art_type for a in artifacts)
            return ok, f"artifact_type_exists({art_type}): {ok}", None

        if ct == AcceptanceCheckType.HUMAN_APPROVAL:
            approval_id = check.params["approval_id"]
            approved = self.repo.is_approval_granted(approval_id)
            return approved, f"human_approval({approval_id}): {approved}", None

        if ct == AcceptanceCheckType.LLM_JUDGE:
            raise NotImplementedError("LLM_JUDGE is V1.2+. Use binary checks in MVP.")

        raise ValueError(f"Unknown check type: {ct}")
```

---

## 10. ValidationGate v0.4.1

### 10.1 目标验证范围

接口从：

```python
validate(task_id, loop_id, candidate)
```

改为：

```python
validate(task_id, loop_id, candidate, target_hunger_item_ids)
```

ValidationGate 验证两类东西：

```text
1. 本轮 target_hunger_item_ids
2. 已经进入 BestState.accepted_check_keys 的 regression checks
```

### 10.2 代码

```python
# src/hungerloop/services/validation_gate.py
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import ValidationReport, CheckResult
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner


def make_check_key(hunger_item_id: str, check_index: int) -> str:
    return f"{hunger_item_id}:{check_index}"


class ValidationGate:
    def __init__(self, repo, acceptance_runner: AcceptanceCheckRunner):
        self.repo = repo
        self.runner = acceptance_runner

    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate,
        target_hunger_item_ids: list[str],
    ) -> ValidationReport:
        best = self.repo.get_best_state(task_id)
        previously_passed = set(best.accepted_check_keys if best else [])

        target_items = self.repo.get_hunger_items(target_hunger_item_ids)

        regression_items = self.repo.get_items_for_check_keys(
            task_id=task_id,
            check_keys=list(previously_passed),
        )

        items_to_check = self._dedupe_items(target_items + regression_items)

        all_results: list[CheckResult] = []
        all_evidence_ids: list[str] = list(candidate.evidence_ids)

        for item in items_to_check:
            for idx, check in enumerate(item.acceptance_checks):
                check_key = make_check_key(item.id, idx)

                # 对 target item：全部检查。
                # 对 regression item：只检查 previously_passed 过的 check，避免扩大验证范围。
                is_target = item.id in target_hunger_item_ids
                is_regression_check = check_key in previously_passed
                if not is_target and not is_regression_check:
                    continue

                passed, detail, ev_id = await self.runner.run(
                    check=check,
                    task_id=task_id,
                    loop_id=loop_id,
                    candidate=candidate,
                )

                previously = check_key in previously_passed
                newly = passed and not previously
                regressed = previously and not passed

                result = CheckResult(
                    hunger_item_id=item.id,
                    check_index=idx,
                    check_key=check_key,
                    check_type=check.check_type.value,
                    passed=passed,
                    previously_passed=previously,
                    newly_passed=newly,
                    regressed=regressed,
                    detail=detail,
                    evidence_id=ev_id,
                    workspace_ref=candidate.workspace_ref,
                )
                all_results.append(result)
                if ev_id:
                    all_evidence_ids.append(ev_id)

        currently_passed = {
            r.check_key for r in all_results if r.passed
        } | {
            key for key in previously_passed
            if key not in {r.check_key for r in all_results}
        }

        newly_passed = [r.check_key for r in all_results if r.newly_passed]
        regressed = [r.check_key for r in all_results if r.regressed]

        satisfied_items, unsatisfied_items = self._aggregate_item_satisfaction(
            target_items=target_items,
            check_results=all_results,
        )

        missing_evidence = []
        if not all_evidence_ids:
            missing_evidence.append("Candidate produced no evidence.")

        has_real_progress = len(newly_passed) > 0

        verdict = self._decide_verdict(
            newly_passed_check_keys=newly_passed,
            regressed_check_keys=regressed,
            missing_evidence=missing_evidence,
            satisfied_hunger_item_ids=satisfied_items,
            unsatisfied_hunger_item_ids=unsatisfied_items,
        )

        return ValidationReport(
            id=f"VAL-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            candidate_state_id=candidate.id,
            baseline_state_id=best.state_id if best else None,
            verdict=verdict,
            attempted_hunger_item_ids=target_hunger_item_ids,
            check_results=all_results,
            currently_passed_check_keys=sorted(currently_passed),
            newly_passed_check_keys=sorted(newly_passed),
            regressed_check_keys=sorted(regressed),
            satisfied_hunger_item_ids=satisfied_items,
            unsatisfied_hunger_item_ids=unsatisfied_items,
            evidence_ids=list(dict.fromkeys(all_evidence_ids)),
            missing_evidence=missing_evidence,
            regressions=[f"check:{k}:regressed" for k in regressed],
            recommended_next_actions=[
                f"Continue working on {iid}" for iid in unsatisfied_items[:3]
            ],
            has_real_progress=has_real_progress,
        )

    def _dedupe_items(self, items):
        seen = set()
        out = []
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                out.append(item)
        return out

    def _aggregate_item_satisfaction(self, target_items, check_results):
        by_item = {}
        for r in check_results:
            by_item.setdefault(r.hunger_item_id, []).append(r)

        satisfied = []
        unsatisfied = []

        for item in target_items:
            results = by_item.get(item.id, [])
            if not results:
                unsatisfied.append(item.id)
                continue

            if item.acceptance_mode == "all":
                ok = all(r.passed for r in results)
            elif item.acceptance_mode == "any":
                ok = any(r.passed for r in results)
            else:
                ok = False

            if ok:
                satisfied.append(item.id)
            else:
                unsatisfied.append(item.id)

        return satisfied, unsatisfied

    def _decide_verdict(
        self,
        newly_passed_check_keys,
        regressed_check_keys,
        missing_evidence,
        satisfied_hunger_item_ids,
        unsatisfied_hunger_item_ids,
    ) -> ValidationVerdict:
        if regressed_check_keys:
            return ValidationVerdict.FAIL

        if missing_evidence:
            return ValidationVerdict.FAIL

        if satisfied_hunger_item_ids and not unsatisfied_hunger_item_ids:
            return ValidationVerdict.PASS

        if newly_passed_check_keys:
            return ValidationVerdict.PARTIAL

        return ValidationVerdict.FAIL
```

---

## 11. CommitManager v0.4.1

### 11.1 新提交规则

```text
Commit if:
  verdict ∈ {PASS, PARTIAL}
  newly_passed_check_keys 非空
  regressed_check_keys 为空
  missing_evidence 为空
```

commit 成功后：

```text
1. candidate workspace promote to best workspace
2. BestState.accepted_check_keys 更新为 validation.currently_passed_check_keys
3. Candidate marked committed
```

commit 失败后：

```text
1. candidate workspace 移到 rejected/
2. Candidate marked rejected
3. FailureBank 写入 validation report
```

### 11.2 代码

```python
# src/hungerloop/services/commit_manager.py
from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import ValidationVerdict


class CommitManager:
    def __init__(self, repo, workspace_manager):
        self.repo = repo
        self.workspace_manager = workspace_manager

    def apply(self, candidate, report):
        if self._can_commit(report):
            self.workspace_manager.promote_candidate_to_best(
                task_id=candidate.task_id,
                loop_id=candidate.loop_id,
            )

            best = BestState(
                task_id=candidate.task_id,
                state_id=candidate.id,
                summary=candidate.summary,
                score=0.0,
                artifact_ids=candidate.artifact_ids,
                evidence_ids=report.evidence_ids,
                validation_id=report.id,
                updated_at_loop=candidate.loop_id,
                accepted_check_keys=report.currently_passed_check_keys,
                workspace_ref="best",
            )

            self.repo.save_best_state(best)
            self.repo.mark_candidate_committed(candidate.id)
            return {
                "committed": True,
                "reason": "validation_passed_with_check_progress",
            }

        self.workspace_manager.reject_candidate(
            task_id=candidate.task_id,
            loop_id=candidate.loop_id,
        )
        self.repo.mark_candidate_rejected(candidate.id)
        self.repo.add_failure_from_validation(report)

        return {
            "committed": False,
            "reason": self._reject_reason(report),
        }

    def _can_commit(self, report) -> bool:
        if report.verdict not in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}:
            return False
        if not report.newly_passed_check_keys:
            return False
        if report.regressed_check_keys:
            return False
        if report.missing_evidence:
            return False
        return True

    def _reject_reason(self, report) -> str:
        if report.verdict == ValidationVerdict.FAIL:
            return "verdict_fail"
        if not report.newly_passed_check_keys:
            return "no_new_check_progress"
        if report.regressed_check_keys:
            return "regressed_checks_detected"
        if report.missing_evidence:
            return "missing_evidence"
        return "unknown"
```

---

## 12. HungerLedger 与 BLOCKED 语义

### 12.1 问题

v0.4：

```python
work_pressure() 排除了 BLOCKED
has_open_items() 也排除了 BLOCKED
```

这会导致所有 item blocked 时：

```text
work_pressure = 0
has_open_items = False
=> DONE
```

### 12.2 v0.4.1 修正

```python
# src/hungerloop/models/hunger.py
class HungerLedger(BaseModel):
    task_id: str
    items: list[HungerItem] = Field(default_factory=list)

    def active_items(self) -> list[HungerItem]:
        return [
            item for item in self.items
            if item.status not in {
                HungerItemStatus.CLOSED,
                HungerItemStatus.PAUSED,
                HungerItemStatus.BLOCKED,
            }
            and item.gap_score > 0
        ]

    def blocked_items(self) -> list[HungerItem]:
        return [
            item for item in self.items
            if item.status == HungerItemStatus.BLOCKED and item.gap_score > 0
        ]

    def unfinished_items(self) -> list[HungerItem]:
        return [
            item for item in self.items
            if item.status not in {
                HungerItemStatus.CLOSED,
                HungerItemStatus.VALIDATED_SATISFIED,
            }
            and item.gap_score > 0
        ]

    def work_pressure(self) -> float:
        return sum(item.priority * item.gap_score for item in self.active_items())

    def has_active_items(self) -> bool:
        return bool(self.active_items())

    def has_blocked_items(self) -> bool:
        return bool(self.blocked_items())

    def all_remaining_items_blocked(self) -> bool:
        unfinished = self.unfinished_items()
        return bool(unfinished) and all(
            item.status == HungerItemStatus.BLOCKED for item in unfinished
        )

    def is_done(self) -> bool:
        return not self.unfinished_items()
```

---

## 13. HungerEngine v0.4.1

### 13.1 Stop 判断顺序

```text
1. HUMAN_PAUSED
2. SAFETY_STOP
3. BLOCKED
4. HUNGER_EXPIRED
5. DONE
```

这避免 blocked 被误判 done。

### 13.2 loop_count decay 语义

定义：

```text
clock.loop_count = 已经完成的 loop 数。
max_loops = policy.decay_duration_seconds when decay_type=LOOP_COUNT。
当 clock.loop_count >= max_loops 时，不再开始新 loop。
```

drive budget：

```python
remaining_loops = max_loops - clock.loop_count
drive_budget = initial_hunger * remaining_loops / max_loops
```

### 13.3 代码

```python
# src/hungerloop/services/hunger_engine.py
class HungerEngine:
    def tick(
        self,
        policy: HungerPolicy,
        clock: HungerClockState,
        ledger: HungerLedger,
        previous_phase: LoopPhase | None = None,
        now: datetime | None = None,
    ) -> HungerSnapshot:
        now = now or datetime.now(timezone.utc)

        drive_budget = self._compute_drive_budget(policy, clock, now)
        drive_budget = max(0.0, min(policy.h_max, drive_budget))

        work_pressure = ledger.work_pressure() * policy.h_max
        active_hunger = min(drive_budget, work_pressure)
        drive_ratio = drive_budget / policy.h_max if policy.h_max > 0 else 0.0
        phase = self._phase_with_hysteresis(drive_ratio, previous_phase)

        should_stop = False
        stop_reason = None

        if clock.frozen:
            should_stop = True
            stop_reason = StopReason.HUMAN_PAUSED

        elif clock.consumed_by_cost_usd >= policy.max_total_cost_usd:
            should_stop = True
            stop_reason = StopReason.SAFETY_STOP

        elif clock.consumed_tokens >= policy.max_total_tokens:
            should_stop = True
            stop_reason = StopReason.SAFETY_STOP

        elif ledger.all_remaining_items_blocked():
            should_stop = True
            stop_reason = StopReason.BLOCKED

        elif drive_budget <= 0 and not ledger.is_done():
            should_stop = True
            stop_reason = StopReason.HUNGER_EXPIRED

        elif ledger.is_done():
            should_stop = True
            stop_reason = StopReason.DONE

        return HungerSnapshot(
            drive_budget=drive_budget,
            work_pressure=work_pressure,
            active_hunger=active_hunger,
            drive_ratio=drive_ratio,
            phase=phase,
            should_stop=should_stop,
            stop_reason=stop_reason,
        )

    def _compute_drive_budget(self, policy, clock, now):
        if clock.manually_cleared:
            return 0.0

        if policy.started_at is None:
            return policy.initial_hunger

        if policy.decay_type == DecayType.LINEAR:
            elapsed = max(0.0, (now - policy.started_at).total_seconds())
            ratio = min(1.0, elapsed / policy.decay_duration_seconds)
            return policy.initial_hunger * (1.0 - ratio)

        if policy.decay_type == DecayType.LOOP_COUNT:
            max_loops = int(policy.decay_duration_seconds)
            remaining = max(0, max_loops - clock.loop_count)
            return policy.initial_hunger * (remaining / max_loops)

        if policy.decay_type == DecayType.STAGE_BASED:
            elapsed = max(0.0, (now - policy.started_at).total_seconds())
            return self._compute_stage_budget(policy, elapsed)

        raise NotImplementedError(f"{policy.decay_type} not in MVP")
```

---

## 14. StagnationDetector v0.4.1

### 14.1 修正点

v0.4 根据所有 unsatisfied_hunger_item_ids 增加失败次数。  
v0.4.1 改为只对本轮 attempted target items 计数。

### 14.2 代码

```python
# src/hungerloop/services/stagnation_detector.py
from hungerloop.models.enums import HungerItemStatus, ValidationVerdict


class StagnationDetector:
    def __init__(
        self,
        repo,
        max_item_consecutive_failures: int = 3,
        max_global_no_progress_loops: int = 5,
    ):
        self.repo = repo
        self.max_item_failures = max_item_consecutive_failures
        self.max_global_no_progress = max_global_no_progress_loops

    def update(self, task_id: str, loop_id: int, validation_report) -> dict:
        attempted = set(validation_report.attempted_hunger_item_ids)
        newly_progressed = set()

        for check_key in validation_report.newly_passed_check_keys:
            item_id = check_key.split(":", 1)[0]
            newly_progressed.add(item_id)

        blocked_items = []

        for iid in attempted:
            item = self.repo.get_hunger_item(iid)
            if item is None:
                continue

            if iid in newly_progressed:
                item.consecutive_failure_count = 0
                item.last_progress_loop_id = loop_id
            else:
                item.consecutive_failure_count += 1

            if item.consecutive_failure_count >= self.max_item_failures:
                item.status = HungerItemStatus.BLOCKED
                blocked_items.append(iid)

            self.repo.save_hunger_item(item)

        if validation_report.has_real_progress:
            self.repo.reset_no_progress_streak(task_id)
            global_blocked = False
        else:
            streak = self.repo.increment_no_progress_streak(task_id)
            global_blocked = streak >= self.max_global_no_progress

        return {
            "blocked_items": blocked_items,
            "global_blocked": global_blocked,
        }
```

---

## 15. HungerUpdateService v0.4.1

### 15.1 gap_score 递减按 check-level progress

v0.4 按 satisfied item 降低 gap。  
v0.4.1 中，一个 item 有多个 checks 时，可以部分降低。

### 15.2 代码

```python
# src/hungerloop/services/hunger_update.py
from hungerloop.models.enums import HungerItemStatus, ValidationVerdict


class HungerUpdateService:
    def __init__(self, repo):
        self.repo = repo

    def apply_validation(self, task_id: str, report) -> None:
        if report.verdict not in {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}:
            return

        progress_by_item: dict[str, int] = {}
        for key in report.newly_passed_check_keys:
            item_id, _ = key.split(":", 1)
            progress_by_item[item_id] = progress_by_item.get(item_id, 0) + 1

        for item_id, new_count in progress_by_item.items():
            item = self.repo.get_hunger_item(item_id)
            if not item:
                continue

            total_checks = max(1, len(item.acceptance_checks))
            decrement = new_count / total_checks

            item.gap_score = max(0.0, item.gap_score - decrement)
            item.evidence_ids.extend(report.evidence_ids)
            item.updated_at_loop = report.loop_id

            if item.id in report.satisfied_hunger_item_ids and item.gap_score == 0.0:
                item.status = HungerItemStatus.VALIDATED_SATISFIED
            else:
                item.status = HungerItemStatus.WORKING

            self.repo.save_hunger_item(item)
```

---

## 16. CostGuard 与 CostTracker v0.4.1

### 16.1 问题

v0.4 只在 HungerEngine tick 时检查 cost ceiling。  
如果一次 loop 内发生很多 LLM/tool 调用，可能中途已经超预算，但要到下一轮才停止。

### 16.2 新设计

引入 `CostGuard`：

```text
LLM 调用前检查
LLM 调用后记录并检查
Tool 调用前检查
Tool 调用后记录并检查
```

### 16.3 代码

```python
# src/hungerloop/services/cost_guard.py
class SafetyStopError(RuntimeError):
    pass


class CostGuard:
    def __init__(self, repo):
        self.repo = repo

    def assert_within_budget(self, task_id: str) -> None:
        policy = self.repo.get_hunger_policy(task_id)
        clock = self.repo.get_hunger_clock(task_id)

        if clock.consumed_by_cost_usd >= policy.max_total_cost_usd:
            raise SafetyStopError(
                f"Cost ceiling hit: ${clock.consumed_by_cost_usd:.4f} >= ${policy.max_total_cost_usd:.4f}"
            )

        if clock.consumed_tokens >= policy.max_total_tokens:
            raise SafetyStopError(
                f"Token ceiling hit: {clock.consumed_tokens} >= {policy.max_total_tokens}"
            )

    def record_llm_usage(self, task_id: str, usage) -> None:
        clock = self.repo.get_hunger_clock(task_id)
        clock.consumed_tokens += usage.input_tokens + usage.output_tokens
        clock.consumed_by_cost_usd += usage.cost_usd
        self.repo.save_hunger_clock(clock)
        self.assert_within_budget(task_id)

    def record_tool_cost(self, task_id: str, cost_usd: float = 0.0, tokens: int = 0) -> None:
        clock = self.repo.get_hunger_clock(task_id)
        clock.consumed_tokens += tokens
        clock.consumed_by_cost_usd += cost_usd
        self.repo.save_hunger_clock(clock)
        self.assert_within_budget(task_id)
```

### 16.4 WorkerRuntime 中使用

```python
class WorkerRuntime:
    async def run(self, spec, context):
        self.cost_guard.assert_within_budget(context.task_id)

        response, usage = await self.llm.complete_json_with_usage(
            prompt=self._build_minimal_prompt(spec, context),
            schema_name=spec.output_schema_name,
            max_tokens=context.budget.get("max_tokens", 4000),
        )

        self.cost_guard.record_llm_usage(context.task_id, usage)

        for call in response.get("tool_calls", []):
            self.cost_guard.assert_within_budget(context.task_id)
            result = await self.tool_harness.execute(...)
            self.cost_guard.assert_within_budget(context.task_id)

        ...
```

### 16.5 Orchestrator 捕获 SafetyStopError

```python
try:
    result = await self.worker_runtime.run(spec, context)
except SafetyStopError as exc:
    self.repo.append_event(
        event_type="safety_stop",
        payload={"task_id": task_id, "loop_id": loop_id, "error": str(exc)},
    )
    return self._build_stop_report(task_id, StopReason.SAFETY_STOP)
```

---

## 17. ToolHarness v0.4.1

### 17.1 workspace_root 必须是 candidate workspace

ToolHarness 不再持有全局 workspace_root。  
每次执行工具时传入 `workspace_root`，也就是本轮 candidate workspace。

```python
async def execute(
    self,
    task_id: str,
    loop_id: int,
    agent_id: str,
    tool_name: str,
    args: dict,
    budget: dict,
    workspace_root: Path,
) -> ToolResult:
    ...
```

### 17.2 path sanitize

```python
def _sanitize_args(self, args: dict, workspace_root: Path) -> dict:
    sanitized = {}
    for k, v in args.items():
        if k in {"path", "cwd", "file"} and isinstance(v, str):
            sanitized[k] = str(resolve_workspace_path(workspace_root, v))
        else:
            sanitized[k] = v
    return sanitized
```

### 17.3 shell_run 工具必须用 argv

```python
class ShellRunTool:
    side_effect_level = "test_or_build"
    default_timeout_seconds = 60

    async def run(self, argv: list[str], cwd: str | None = None):
        if not isinstance(argv, list) or not argv:
            raise ValueError("shell_run requires argv: list[str]")
        ...
```

不接受：

```json
{"cmd": "pytest tests/"}
```

接受：

```json
{"argv": ["pytest", "tests/"]}
```

---

## 18. ContextBuilder v0.4.1

ContextPack 增加 candidate workspace 信息。

```python
class ContextPack(BaseModel):
    task_id: str
    loop_id: int
    agent_id: str
    mission: str
    phase: str

    target_hunger_item_ids: list[str]
    acceptance_criteria: list[str]

    best_state_summary: str | None = None
    best_workspace_ref: str = "best"
    candidate_workspace_ref: str

    relevant_claim_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    failure_patterns_to_avoid: list[str] = Field(default_factory=list)

    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)

    budget: dict = Field(default_factory=dict)
    required_output_schema: str
```

构造：

```python
candidate_workspace_ref = f"candidates/loop_{loop_id:03d}"

return ContextPack(
    ...,
    candidate_workspace_ref=candidate_workspace_ref,
)
```

Worker prompt 中明确：

```text
You may only write files inside candidate_workspace_ref.
Never assume changes are committed until ValidationGate passes.
```

但注意：

> 这只是提示。真正的 enforcement 在 ToolHarness / WorkspaceManager / path safety。

---

## 19. Integrator v0.4.1

CandidateState 必须带 workspace_ref。

```python
class Integrator:
    def integrate(self, task_id: str, loop_id: int, results: list[WorkerResult]) -> CandidateState:
        ...
        return CandidateState(
            id=f"CAND-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            summary="\n".join(summaries),
            artifact_ids=list(dict.fromkeys(artifact_ids)),
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            claim_ids=list(dict.fromkeys(claim_ids)),
            proposed_score=0.0,
            workspace_ref=f"candidates/loop_{loop_id:03d}",
        )
```

---

## 20. RequirementCompiler v0.4.1

### 20.1 Memory consolidation 默认不作为 active item

v0.4 默认创建 H-003：

```text
Memory consolidation
HUMAN_APPROVAL
```

这会导致很多任务无法自动 DONE。

v0.4.1 改为：

```text
MemoryCandidate 生成是系统行为。
Memory promotion 是人类后处理。
Memory consolidation 默认不作为 active HungerItem。
只有用户显式 enable_memory_consolidation 时才创建 H-003。
```

### 20.2 代码

```python
class RuleBasedCompiler:
    def compile(self, task_id: str, raw_goal: str, hints: dict | None = None):
        hints = hints or {}
        items = []

        core_checks = hints.get("core_acceptance_checks", [])
        if not core_checks:
            raise ValueError("MVP requires core_acceptance_checks.")

        items.append(HungerItem(
            id="H-001",
            title="Core deliverable",
            item_type=HungerItemType.GOAL_GAP,
            priority=1.0,
            gap_score=1.0,
            acceptance_checks=[AcceptanceCheck(**c) for c in core_checks],
            acceptance_mode=hints.get("core_acceptance_mode", "all"),
        ))

        items.append(HungerItem(
            id="H-002",
            title="Sufficient evidence",
            item_type=HungerItemType.GOAL_GAP,
            priority=0.7,
            gap_score=1.0,
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.EVIDENCE_COUNT_MIN,
                    params={"evidence_type": "any", "min_count": 1},
                    description="At least one evidence item.",
                )
            ],
            acceptance_mode="all",
        ))

        if hints.get("enable_memory_consolidation", False):
            items.append(HungerItem(
                id="H-003",
                title="Memory consolidation",
                item_type=HungerItemType.MEMORY_CONSOLIDATION,
                priority=0.4,
                gap_score=1.0,
                status=HungerItemStatus.OPEN,
                acceptance_checks=[
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.HUMAN_APPROVAL,
                        params={"approval_id": f"{task_id}-memory"},
                        description="Human approves promoted memory.",
                    )
                ],
                acceptance_mode="all",
            ))

        return goal, HungerLedger(task_id=task_id, items=items)
```

---

## 21. Orchestrator v0.4.1

### 21.1 新流程

```text
1. tick hunger
2. stop check
3. create candidate workspace from best
4. allocate budget
5. plan
6. build context with candidate workspace
7. worker executes inside candidate workspace
8. integrate candidate
9. validate target items + regression checks inside candidate workspace
10. commit or reject workspace
11. update hunger by check-level progress
12. update stagnation on attempted target items
13. propose memory candidate
14. increment loop_count
15. write trace
```

### 21.2 代码

```python
class LoopOrchestrator:
    async def step(self, task_id: str) -> LoopTrace | StopReport:
        loop_id = self.repo.next_loop_id(task_id)

        policy = self.repo.get_hunger_policy(task_id)
        clock = self.repo.get_hunger_clock(task_id)
        ledger = self.repo.get_hunger_ledger(task_id)
        previous_phase = self.repo.get_last_phase(task_id)

        snapshot = self.hunger_engine.tick(
            policy=policy,
            clock=clock,
            ledger=ledger,
            previous_phase=previous_phase,
        )
        self.repo.save_hunger_snapshot(task_id, snapshot)

        if snapshot.should_stop:
            return self._build_stop_report(task_id, snapshot.stop_reason)

        # NEW: create candidate workspace before planning/execution
        candidate_root = self.workspace_manager.create_candidate_workspace(
            task_id=task_id,
            loop_id=loop_id,
        )

        budget = self.budget_allocator.allocate(snapshot)
        plan = self.planner.plan(task_id, loop_id, snapshot, budget)
        self.repo.save_loop_plan(plan)

        if not plan.assignments:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            return self._build_stop_report(task_id, StopReason.BLOCKED)

        worker_results = []

        try:
            for assignment in plan.assignments:
                spec = self.repo.get_agent_spec(assignment.agent_id)

                context = self.context_builder.build_for_agent(
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

                result = await self.worker_runtime.run(
                    spec=spec,
                    context=context,
                    workspace_root=candidate_root,
                )
                worker_results.append(result)
                self.repo.save_worker_result(result)

        except SafetyStopError:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            return self._build_stop_report(task_id, StopReason.SAFETY_STOP)

        except Exception as exc:
            self.workspace_manager.reject_candidate(task_id, loop_id)
            self.repo.append_event(
                event_type="worker_error",
                payload={"task_id": task_id, "loop_id": loop_id, "error": str(exc)},
            )
            return self._build_stop_report(task_id, StopReason.ERROR)

        candidate = self.integrator.integrate(task_id, loop_id, worker_results)
        self.repo.save_candidate(candidate)

        validation = await self.validation_gate.validate(
            task_id=task_id,
            loop_id=loop_id,
            candidate=candidate,
            target_hunger_item_ids=plan.selected_hunger_item_ids,
        )
        self.repo.save_validation_report(validation)

        commit_result = self.commit_manager.apply(candidate, validation)

        self.hunger_update_service.apply_validation(task_id, validation)

        stagnation = self.stagnation_detector.update(task_id, loop_id, validation)
        if stagnation["global_blocked"]:
            return self._build_stop_report(task_id, StopReason.BLOCKED)

        self.memory_manager.propose_from_loop(task_id, loop_id, validation)

        clock = self.repo.get_hunger_clock(task_id)
        clock.loop_count += 1
        self.repo.save_hunger_clock(clock)

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
            delta_summary=self._build_delta_summary(validation, commit_result),
            blocked_item_ids=stagnation["blocked_items"],
            next_action="continue",
        )
        self.repo.save_loop_trace(trace)
        return trace
```

---

## 22. Storage Schema 变更

### 22.1 新增表：accepted_checks

```sql
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

### 22.2 validation_reports payload 中新增字段

```text
attempted_hunger_item_ids
currently_passed_check_keys
newly_passed_check_keys
regressed_check_keys
```

### 22.3 best_states payload 中新增字段

```text
accepted_check_keys
workspace_ref
```

### 22.4 candidates payload 中新增字段

```text
workspace_ref
```

### 22.5 workspace_manifests

```sql
CREATE TABLE workspace_manifests (
  workspace_ref TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  loop_id INTEGER,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  source_workspace_ref TEXT,
  payload_json TEXT NOT NULL
);
```

---

## 23. CLI 变更

### 23.1 `--accept` 必须使用安全格式

旧格式：

```bash
--accept "shell_exit_zero:cmd=pytest tests/test_foo.py:timeout=60"
```

v0.4.1 推荐：

```bash
--accept 'shell_exit_zero:argv=["pytest","tests/test_foo.py"]:timeout=60'
```

为了 CLI 易用，可以支持简写：

```bash
--accept "pytest:tests/test_foo.py"
```

由 CLI 转成：

```json
{
  "check_type": "shell_exit_zero",
  "params": {
    "argv": ["pytest", "tests/test_foo.py"],
    "timeout": 60
  }
}
```

### 23.2 新增 workspace inspect

```bash
hungerloop workspace best <task_id>
hungerloop workspace candidate <task_id> --loop 3
hungerloop workspace rejected <task_id> --loop 3
hungerloop diff <task_id> --loop 3
```

### 23.3 新增 accepted checks inspect

```bash
hungerloop checks <task_id>
```

输出：

```text
H-001:0  PASS  loop=2  file_exists: report.md
H-001:1  PASS  loop=4  shell_exit_zero: pytest tests/
H-002:0  PASS  loop=1  evidence_count_min
```

---

## 24. 测试计划更新

### 24.1 新增 P0 单元测试

```text
test_check_level_progress.py
  - 单 item 多 check，第一个 check 通过时 has_real_progress=True
  - 第二个 check 后续通过时 again has_real_progress=True
  - 没有新增 check 通过时 has_real_progress=False

test_commit_manager_v041.py
  - newly_passed_check_keys 为空不能 commit
  - regressed_check_keys 非空不能 commit
  - missing_evidence 非空不能 commit
  - PASS + newly_passed_check_keys 可以 commit
  - PARTIAL + newly_passed_check_keys 可以 commit
  - commit 成功会 promote candidate workspace
  - commit 失败会 reject candidate workspace

test_workspace_isolation.py
  - worker 写 candidate workspace 不影响 best workspace
  - commit 后 best workspace 更新
  - reject 后 best workspace 不变
  - rejected candidate 被移动到 rejected/

test_targeted_validation.py
  - 本轮只验证 target hunger item
  - 未 target 的 open item 不计入 unsatisfied
  - previously passed check 被作为 regression check 验证

test_blocked_semantics.py
  - 所有剩余 item BLOCKED 时 StopReason.BLOCKED
  - 所有 item CLOSED/VALIDATED_SATISFIED 时 StopReason.DONE
  - BLOCKED 不会被误判 DONE

test_path_safety.py
  - 绝对路径被拒绝
  - ../ 逃逸被拒绝
  - candidate workspace 内相对路径允许

test_sandbox_runner.py
  - argv 执行成功
  - timeout 返回 timed_out=True
  - stdout/stderr 被截断
  - shell 字符串不被接受

test_cost_guard.py
  - LLM 调用前超预算直接 SafetyStopError
  - LLM 调用后超预算立即 SafetyStopError
  - Tool 调用前后都检查预算

test_loop_count_decay.py
  - loop_count=0 时预算为 initial_hunger
  - loop_count=max_loops-1 时还能开始最后一轮
  - loop_count=max_loops 时 HUNGER_EXPIRED 或 DONE
```

---

### 24.2 更新集成测试

#### test_demo_partial_check_progress.py

```text
Given:
  H-001 有两个 checks:
    - file_exists report.md
    - shell_exit_zero pytest tests/
When:
  loop 1 只创建 report.md，但 pytest 还失败
Then:
  ValidationReport.verdict == PARTIAL
  newly_passed_check_keys == ["H-001:0"]
  commit == True
  BestState.accepted_check_keys 包含 H-001:0
```

#### test_demo_reject_does_not_pollute_best_workspace.py

```text
Given:
  best workspace 中 app.py 是稳定版本
When:
  loop 2 candidate 修改 app.py 但 validation fail
Then:
  best/files/app.py 不变
  rejected/loop_002/files/app.py 存在
  candidate marked rejected
```

#### test_demo_regression_blocks_commit.py

```text
Given:
  H-001:0 已通过
When:
  candidate 删除对应文件
Then:
  regressed_check_keys 包含 H-001:0
  commit == False
  StopReason 不一定 BLOCKED，但 FailureBank 记录 regression
```

---

## 25. 迁移清单：从 v0.4 到 v0.4.1

### 25.1 必改

```text
1. 删除 I-3 中 score_after > score_before 的表述。
2. ValidationReport 增加 check-level 字段。
3. BestState 增加 accepted_check_keys 和 workspace_ref。
4. CandidateState 增加 workspace_ref。
5. CommitManager 改成 check-level commit。
6. 引入 WorkspaceManager。
7. WorkerRuntime / ToolHarness 增加 workspace_root 参数。
8. AcceptanceCheckRunner 改用 SandboxRunner。
9. ValidationGate 增加 target_hunger_item_ids 参数。
10. StagnationDetector 只统计 attempted items。
11. HungerLedger 增加 active_items / blocked_items / unfinished_items / is_done。
12. HungerEngine 修正 BLOCKED / DONE 判断顺序。
13. CostGuard 加到 LLM/tool 调用前后。
14. RuleBasedCompiler 默认不创建 active memory H-003。
```

### 25.2 可后置但建议同时做

```text
1. CLI 增加 workspace inspect。
2. CLI 增加 checks inspect。
3. JSONL event log 记录 workspace_promoted / workspace_rejected。
4. accepted_checks 表。
5. candidate vs best diff。
```

---

## 26. v0.4.1 后的 MVP 成功标准

v0.4.1 后，MVP 成功标准更新为：

```text
1. Agent 不能自己续命。
2. Agent 不能绕过 ValidationGate 提交。
3. Worker 不能直接修改 BestState。
4. Worker 只能写 candidate workspace。
5. Rejected candidate 不能污染 best workspace。
6. Commit 不依赖 score，而依赖 newly_passed_check_keys。
7. 已通过 check 不能在 BestState 中回退。
8. ValidationGate 不会惩罚未尝试的 open item。
9. StagnationDetector 不会把未尝试 item 错标 BLOCKED。
10. BLOCKED 不会被误判 DONE。
11. Acceptance shell check 不使用 shell=True。
12. path 不能逃逸 candidate workspace。
13. cost ceiling 可在 loop 中途触发 SAFETY_STOP。
14. loop_count decay 允许精确控制最大 loop 数。
15. Memory consolidation 不会默认阻塞 DONE。
```

---

## 27. 推荐实现顺序

不要在 v0.4.1 里增加聪明功能。  
先修工程底座。

```text
Day 1:
  - CheckKey / CheckResult / ValidationReport 模型
  - BestState.accepted_check_keys
  - ValidationGate check-level progress

Day 2:
  - WorkspaceManager
  - Candidate workspace lifecycle
  - CommitManager promote/reject

Day 3:
  - Path safety
  - SandboxRunner
  - AcceptanceCheckRunner 改造

Day 4:
  - Targeted validation
  - StagnationDetector attempted-only
  - BLOCKED semantics

Day 5:
  - CostGuard
  - loop_count decay 修正
  - RuleBasedCompiler memory item optional

Day 6:
  - CLI workspace/checks inspect
  - migration schema

Day 7:
  - 新增测试全部跑通
  - 更新 README / examples
```

---

## 28. 关键伪代码：一轮 loop

```python
async def step(task_id: str):
    loop_id = repo.next_loop_id(task_id)

    policy = repo.get_hunger_policy(task_id)
    clock = repo.get_hunger_clock(task_id)
    ledger = repo.get_hunger_ledger(task_id)

    snapshot = hunger_engine.tick(policy, clock, ledger)
    if snapshot.should_stop:
        return stop_report(snapshot.stop_reason)

    candidate_root = workspace_manager.create_candidate_workspace(task_id, loop_id)

    budget = budget_allocator.allocate(snapshot)
    plan = planner.plan(task_id, loop_id, snapshot, budget)

    if not plan.assignments:
        workspace_manager.reject_candidate(task_id, loop_id)
        return stop_report(StopReason.BLOCKED)

    results = []
    for assignment in plan.assignments:
        context = context_builder.build_for_agent(
            ...,
            candidate_workspace_ref=f"candidates/loop_{loop_id:03d}",
        )
        result = await worker_runtime.run(
            spec=repo.get_agent_spec(assignment.agent_id),
            context=context,
            workspace_root=candidate_root,
        )
        results.append(result)

    candidate = integrator.integrate(task_id, loop_id, results)

    validation = await validation_gate.validate(
        task_id=task_id,
        loop_id=loop_id,
        candidate=candidate,
        target_hunger_item_ids=plan.selected_hunger_item_ids,
    )

    commit = commit_manager.apply(candidate, validation)

    hunger_update.apply_validation(task_id, validation)
    stagnation = stagnation_detector.update(task_id, loop_id, validation)

    if stagnation["global_blocked"]:
        return stop_report(StopReason.BLOCKED)

    memory_manager.propose_from_loop(task_id, loop_id, validation)

    clock.loop_count += 1
    repo.save_hunger_clock(clock)

    return loop_trace(...)
```

---

## 29. v0.4.1 不做的事

为了避免范围再次膨胀，v0.4.1 明确不做：

```text
1. 不做 3×3 Worker。
2. 不做 LLMPlanner。
3. 不做 LLM-as-judge。
4. 不做自动 Memory Promotion。
5. 不做 FastAPI。
6. 不做 EXPONENTIAL / COST_BASED / MANUAL decay。
7. 不做复杂浏览器自动化。
8. 不做跨任务全局 memory recall。
9. 不做多 worker 并发 conflict resolution。
10. 不做复杂 artifact diff UI。
```

v0.4.1 是工程修复版，不是功能扩展版。

---

## 30. 给开发者的最终提醒

如果只实现 v0.4 而不做 v0.4.1，会出现几个很隐蔽但严重的问题：

```text
1. 数据库里的 BestState 看起来没回退，但文件系统已经被 rejected candidate 污染。
2. 一个 item 多个 checks 时，部分真实进展无法 commit。
3. 没被本轮尝试的 item 会被错误算作失败。
4. 所有 item blocked 时可能被误判 DONE。
5. shell acceptance check 绕过安全层。
6. cost ceiling 可能要到下一轮才触发。
7. memory approval 可能让简单任务永远无法 DONE。
```

v0.4.1 的重点不是让系统更强，而是让系统**不会自欺、不会污染、不会误停、不会误判、不会失控**。

---

## 31. v0.4.1 最终判断

v0.4 是正确的 MVP 方向。  
v0.4.1 是实现前必须补上的工程安全层。

一句话总结：

> **v0.4 定义了真实 MVP 的形状。  
> v0.4.1 让这个 MVP 可以安全地写代码。**

