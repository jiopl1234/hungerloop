"""SQLite-backed RepositoryProtocol implementation for HungerLoop."""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from hungerloop.models.blackboard import Artifact, BestState, CandidateState
from hungerloop.models.enums import EvidenceType, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.planning import LoopPlan
from hungerloop.models.skill import SkillCard
from hungerloop.models.task import TaskRecord
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.usage import UsageSnapshot
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository import migrations as migrations_pkg
from hungerloop.repository.evidence_success import is_successful_evidence_payload
from hungerloop.repository.sqlite_migrator import SQLiteMigrator


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _loads(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stored payload_json must be an object")
    return value


def _model_json(model: BaseModel) -> str:
    return model.model_dump_json()


def _same_host_pid(owner_a: str, owner_b: str) -> bool:
    return owner_a.rsplit(":", 1)[0] == owner_b.rsplit(":", 1)[0]


class SQLiteRepository:
    """SQLite implementation of :class:`RepositoryProtocol`.

    The SQL schema keeps a few query columns plus a full Pydantic
    ``payload_json`` for lossless round-tripping. This mirrors
    ``InMemoryRepository`` behavior while making the production CLI durable.
    """

    def __init__(self, db_path: Path, *, write_capable: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrations_dir = Path(migrations_pkg.__file__).parent
        SQLiteMigrator(self.db_path, migrations_dir).ensure_current(
            write_capable=write_capable
        )
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._clock_task_ids: dict[int, str] = {}
        self._item_task_ids: dict[str, str] = {}
        self._snapshot_loop_ids: dict[tuple[str, int], int] = {}
        self._tx_depth = 0

    @classmethod
    def open(cls, db_path: Path, *, write_capable: bool = True) -> SQLiteRepository:
        return cls(db_path, write_capable=write_capable)

    def close(self) -> None:
        self.conn.close()

    # =====================================================================
    # Section 0 — Task metadata
    # =====================================================================
    def create_task(self, task_id: str, raw_goal: str) -> None:
        now = _utc_now()
        self.conn.execute(
            """
            INSERT INTO tasks(task_id, goal, status, last_stop_reason, created_at, updated_at)
            VALUES (?, ?, 'pending', NULL, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              goal=excluded.goal,
              updated_at=excluded.updated_at
            """,
            (task_id, raw_goal, now, now),
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self.conn.execute(
            """
            SELECT task_id, goal, status, last_stop_reason, created_at, updated_at
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task_id=str(row["task_id"]),
            raw_goal=str(row["goal"]),
            status=str(row["status"]),
            last_stop_reason=(
                StopReason(str(row["last_stop_reason"]))
                if row["last_stop_reason"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def task_exists(self, task_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM tasks WHERE task_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if row is not None:
            return True
        for table in (
            "hunger_policies",
            "hunger_ledgers",
            "best_states",
            "stop_reports",
        ):
            row = self.conn.execute(
                f"SELECT 1 FROM {table} WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone()
            if row is not None:
                return True
        row = self.conn.execute(
            "SELECT 1 FROM loop_traces WHERE task_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        return row is not None

    def set_hunger_policy(self, task_id: str, policy: HungerPolicy) -> None:
        self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO hunger_policies(task_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (task_id, _model_json(policy)),
        )

    # =====================================================================
    # Section 1 — Hunger
    # =====================================================================
    def get_hunger_policy(self, task_id: str) -> HungerPolicy:
        row = self.conn.execute(
            "SELECT payload_json FROM hunger_policies WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return HungerPolicy()
        return HungerPolicy.model_validate(_loads(str(row["payload_json"])))

    def get_hunger_clock(self, task_id: str) -> HungerClockState:
        row = self.conn.execute(
            "SELECT payload_json FROM hunger_clocks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            clock = HungerClockState()
            self._clock_task_ids[id(clock)] = task_id
            return clock
        clock = HungerClockState.model_validate(_loads(str(row["payload_json"])))
        self._clock_task_ids[id(clock)] = task_id
        return clock

    def save_hunger_clock(self, clock: HungerClockState) -> None:
        task_id = self._task_id_for_clock(clock)
        self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO hunger_clocks(task_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (task_id, _model_json(clock)),
        )
        self._upsert_usage(
            UsageSnapshot(
                task_id=task_id,
                tokens=clock.consumed_tokens,
                cost_usd=clock.consumed_by_cost_usd,
                llm_calls=self.get_usage_snapshot(task_id).llm_calls,
                tool_calls=self.get_usage_snapshot(task_id).tool_calls,
            )
        )

    def get_hunger_ledger(self, task_id: str) -> HungerLedger:
        row = self.conn.execute(
            "SELECT payload_json FROM hunger_ledgers WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return HungerLedger(task_id=task_id, items=[])
        ledger = HungerLedger.model_validate(_loads(str(row["payload_json"])))
        for item in ledger.items:
            self._item_task_ids[item.id] = task_id
        return ledger

    def save_hunger_ledger(self, task_id: str, ledger: HungerLedger) -> None:
        self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO hunger_ledgers(task_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (task_id, _model_json(ledger)),
        )
        for item in ledger.items:
            self._save_hunger_item_for_task(task_id, item)

    def get_hunger_item(self, item_id: str) -> HungerItem | None:
        row = self.conn.execute(
            """
            SELECT task_id, payload_json
            FROM hunger_items
            WHERE item_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        task_id = str(row["task_id"])
        item = HungerItem.model_validate(_loads(str(row["payload_json"])))
        self._item_task_ids[item.id] = task_id
        return item

    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]:
        return [item for iid in item_ids if (item := self.get_hunger_item(iid))]

    def save_hunger_item(self, item: HungerItem) -> None:
        task_id = self._task_id_for_item(item)
        self._save_hunger_item_for_task(task_id, item)
        ledger = self.get_hunger_ledger(task_id)
        updated = [
            item if existing.id == item.id else existing for existing in ledger.items
        ]
        self.conn.execute(
            """
            UPDATE hunger_ledgers
            SET payload_json = ?
            WHERE task_id = ?
            """,
            (_model_json(HungerLedger(task_id=task_id, items=updated)), task_id),
        )

    def get_items_for_check_keys(
        self, task_id: str, check_keys: list[str]
    ) -> list[HungerItem]:
        item_ids = [key.split(":", 1)[0] for key in check_keys]
        rows = self.conn.execute(
            f"""
            SELECT payload_json
            FROM hunger_items
            WHERE task_id = ?
              AND item_id IN ({",".join("?" for _ in item_ids) if item_ids else "NULL"})
            """,
            (task_id, *item_ids),
        ).fetchall()
        return [
            HungerItem.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None:
        self._ensure_task(task_id)
        loop_id = self._current_loop_id(task_id)
        self._snapshot_loop_ids[(task_id, id(snapshot))] = loop_id
        self.conn.execute(
            """
            INSERT INTO hunger_snapshots(task_id, loop_id, phase, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id, loop_id) DO UPDATE SET
              phase=excluded.phase,
              payload_json=excluded.payload_json
            """,
            (task_id, loop_id, snapshot.phase.value, _model_json(snapshot)),
        )

    def get_last_phase(self, task_id: str) -> LoopPhase | None:
        row = self.conn.execute(
            """
            SELECT phase FROM hunger_snapshots
            WHERE task_id = ?
            ORDER BY loop_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return LoopPhase(str(row["phase"])) if row else None

    def get_latest_hunger_snapshot(self, task_id: str) -> HungerSnapshot | None:
        row = self.conn.execute(
            """
            SELECT payload_json FROM hunger_snapshots
            WHERE task_id = ?
            ORDER BY loop_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return HungerSnapshot.model_validate(_loads(str(row["payload_json"])))

    # =====================================================================
    # Section 2 — Workspace state
    # =====================================================================
    def get_best_state(self, task_id: str) -> BestState | None:
        row = self.conn.execute(
            "SELECT payload_json FROM best_states WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return BestState.model_validate(_loads(str(row["payload_json"])))

    def save_best_state(self, best: BestState) -> None:
        self._ensure_task(best.task_id)
        self.conn.execute(
            """
            INSERT INTO best_states(task_id, state_id, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              state_id=excluded.state_id,
              payload_json=excluded.payload_json
            """,
            (best.task_id, best.state_id, _model_json(best)),
        )

    def save_candidate(self, candidate: CandidateState) -> None:
        self._ensure_task(candidate.task_id)
        self.conn.execute(
            """
            INSERT INTO candidates(candidate_id, task_id, loop_id, status, payload_json)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
              status=candidates.status,
              payload_json=excluded.payload_json
            """,
            (
                candidate.id,
                candidate.task_id,
                candidate.loop_id,
                _model_json(candidate),
            ),
        )

    def mark_candidate_committed(self, candidate_id: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET status = 'committed' WHERE candidate_id = ?",
            (candidate_id,),
        )

    def mark_candidate_rejected(self, candidate_id: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET status = 'rejected' WHERE candidate_id = ?",
            (candidate_id,),
        )

    def list_candidates_for_task(self, task_id: str) -> list[CandidateState]:
        rows = self.conn.execute(
            """
            SELECT payload_json FROM candidates
            WHERE task_id = ?
            ORDER BY loop_id ASC, candidate_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            CandidateState.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    def get_candidate(self, candidate_id: str) -> CandidateState | None:
        row = self.conn.execute(
            "SELECT payload_json FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return CandidateState.model_validate(_loads(str(row["payload_json"])))

    # =====================================================================
    # Section 3 — Validation
    # =====================================================================
    def save_validation_report(self, report: ValidationReport) -> None:
        self._ensure_task(report.task_id)
        self.conn.execute(
            """
            INSERT INTO validation_reports(
              validation_id, task_id, loop_id, verdict, payload_json
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(validation_id) DO UPDATE SET
              verdict=excluded.verdict,
              payload_json=excluded.payload_json
            """,
            (
                report.id,
                report.task_id,
                report.loop_id,
                report.verdict.value,
                _model_json(report),
            ),
        )

    def add_failure_from_validation(self, report: ValidationReport) -> None:
        self.save_validation_report(report)

    def get_validation_report(
        self, validation_id: str
    ) -> ValidationReport | None:
        row = self.conn.execute(
            "SELECT payload_json FROM validation_reports WHERE validation_id = ?",
            (validation_id,),
        ).fetchone()
        if row is None:
            return None
        return ValidationReport.model_validate(_loads(str(row["payload_json"])))

    def validation_exists(self, validation_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM validation_reports WHERE validation_id = ? LIMIT 1",
            (validation_id,),
        ).fetchone()
        return row is not None

    def iter_accepted_checks(self, task_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT check_key, hunger_item_id, check_index,
                   accepted_at_loop, validation_id, evidence_id
            FROM accepted_checks
            WHERE task_id = ?
            ORDER BY check_key ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            {
                "check_key": str(row["check_key"]),
                "hunger_item_id": str(row["hunger_item_id"]),
                "check_index": int(row["check_index"]),
                "accepted_at_loop": int(row["accepted_at_loop"]),
                "validation_id": str(row["validation_id"]),
                "evidence_id": (
                    str(row["evidence_id"])
                    if row["evidence_id"] is not None
                    else None
                ),
            }
            for row in rows
        ]

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
    ) -> None:
        self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO accepted_checks(
              task_id, check_key, hunger_item_id, check_index,
              accepted_at_loop, validation_id, evidence_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, check_key) DO UPDATE SET
              hunger_item_id=excluded.hunger_item_id,
              check_index=excluded.check_index,
              accepted_at_loop=excluded.accepted_at_loop,
              validation_id=excluded.validation_id,
              evidence_id=excluded.evidence_id
            """,
            (
                task_id,
                check_key,
                hunger_item_id,
                check_index,
                accepted_at_loop,
                validation_id,
                evidence_id,
            ),
        )

    # =====================================================================
    # Section 4 — Evidence
    # =====================================================================
    def save_shell_output_as_evidence(
        self,
        task_id: str,
        loop_id: int,
        label: str,
        argv: list[str],
        cwd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> str:
        payload: dict[str, object] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "label": label,
            "argv": argv,
            "cwd": cwd,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "type": EvidenceType.SANDBOX_RUN.value,
        }
        return self._insert_evidence(
            task_id, loop_id, EvidenceType.SANDBOX_RUN, payload
        )

    def save_model_call_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        response_preview: str,
    ) -> str:
        payload: dict[str, object] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "response_preview": response_preview,
            "type": EvidenceType.MODEL_CALL.value,
        }
        eid = self._insert_evidence(task_id, loop_id, EvidenceType.MODEL_CALL, payload)
        usage = self.get_usage_snapshot(task_id)
        usage.llm_calls += 1
        usage.tokens += input_tokens + output_tokens
        usage.cost_usd += cost_usd
        self._upsert_usage(usage)
        return eid

    def save_model_error_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        agent_id: str,
        provider: str,
        model: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> str:
        payload: dict[str, object] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "error_type": error_type,
            "error_message": error_message,
            "retryable": retryable,
            "type": EvidenceType.MODEL_ERROR.value,
        }
        return self._insert_evidence(task_id, loop_id, EvidenceType.MODEL_ERROR, payload)

    def save_tool_call_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        tool_name: str,
        args_summary: str,
        result_summary: str,
        success: bool,
        elapsed_ms: int,
    ) -> str:
        payload: dict[str, object] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "args_summary": args_summary,
            "result_summary": result_summary,
            "success": success,
            "elapsed_ms": elapsed_ms,
            "type": EvidenceType.TOOL_CALL.value,
        }
        eid = self._insert_evidence(task_id, loop_id, EvidenceType.TOOL_CALL, payload)
        usage = self.get_usage_snapshot(task_id)
        usage.tool_calls += 1
        self._upsert_usage(usage)
        return eid

    def count_evidence_by_type(
        self,
        task_id: str,
        evidence_ids: list[str],
        evidence_type: EvidenceType | str,
        *,
        successful_only: bool = False,
    ) -> int:
        if not evidence_ids:
            return 0
        wanted = (
            evidence_type.value
            if isinstance(evidence_type, EvidenceType)
            else evidence_type
        )
        placeholders = ",".join("?" for _ in evidence_ids)
        type_clause = "" if wanted == "any" else "AND evidence_type = ?"
        params: tuple[object, ...] = (
            (task_id, *evidence_ids)
            if wanted == "any"
            else (task_id, *evidence_ids, wanted)
        )
        rows = self.conn.execute(
            f"""
            SELECT evidence_type, payload_json
            FROM evidence
            WHERE task_id = ?
              AND evidence_id IN ({placeholders})
              {type_clause}
            """,
            params,
        ).fetchall()
        count = 0
        for row in rows:
            actual_type = str(row["evidence_type"])
            if successful_only and not is_successful_evidence_payload(
                actual_type, _loads(str(row["payload_json"]))
            ):
                continue
            count += 1
        return count

    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[Artifact]:
        if not artifact_ids:
            return []
        rows = self.conn.execute(
            f"""
            SELECT payload_json FROM artifacts
            WHERE artifact_id IN ({",".join("?" for _ in artifact_ids)})
            """,
            tuple(artifact_ids),
        ).fetchall()
        return [
            Artifact.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    def save_artifact(self, artifact: Artifact) -> None:
        self._ensure_task(artifact.task_id)
        self.conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, task_id, loop_id, artifact_type, path, summary, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              artifact_type=excluded.artifact_type,
              path=excluded.path,
              summary=excluded.summary,
              payload_json=excluded.payload_json
            """,
            (
                artifact.artifact_id,
                artifact.task_id,
                artifact.loop_id,
                artifact.artifact_type,
                artifact.path,
                artifact.summary,
                _model_json(artifact),
            ),
        )

    # =====================================================================
    # Section 5 — Worker / Planning
    # =====================================================================
    def get_agent_spec(self, agent_id: str) -> AgentSpec:
        row = self.conn.execute(
            "SELECT payload_json FROM agent_specs WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"AgentSpec not registered: {agent_id}")
        return AgentSpec.model_validate(_loads(str(row["payload_json"])))

    def save_agent_spec(self, spec: AgentSpec) -> None:
        self.conn.execute(
            """
            INSERT INTO agent_specs(agent_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (spec.agent_id, _model_json(spec)),
        )

    def save_worker_result(self, result: WorkerResult) -> None:
        self._ensure_task(result.task_id)
        rid = f"WR-{result.task_id}-{result.loop_id}-{result.agent_id}"
        self.conn.execute(
            """
            INSERT INTO worker_results(result_id, task_id, loop_id, agent_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(result_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (
                rid,
                result.task_id,
                result.loop_id,
                result.agent_id,
                _model_json(result),
            ),
        )

    def save_loop_plan(self, plan: LoopPlan) -> None:
        self._ensure_task(plan.task_id)
        self.conn.execute(
            """
            INSERT INTO loop_plans(task_id, loop_id, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id, loop_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (plan.task_id, plan.loop_id, _model_json(plan)),
        )

    # =====================================================================
    # Section 6 — Trace / Stop
    # =====================================================================
    def save_loop_trace(self, trace: LoopTrace) -> None:
        self._ensure_task(trace.task_id)
        self.conn.execute(
            """
            INSERT INTO loop_traces(task_id, loop_id, phase, committed, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, loop_id) DO UPDATE SET
              phase=excluded.phase,
              committed=excluded.committed,
              payload_json=excluded.payload_json
            """,
            (
                trace.task_id,
                trace.loop_id,
                trace.phase,
                int(trace.committed),
                _model_json(trace),
            ),
        )

    def list_loop_traces(self, task_id: str) -> list[LoopTrace]:
        rows = self.conn.execute(
            """
            SELECT payload_json FROM loop_traces
            WHERE task_id = ?
            ORDER BY loop_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            LoopTrace.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    def save_stop_report(self, report: StopReport) -> None:
        self._ensure_task(report.task_id)
        now = _utc_now()
        self.conn.execute(
            """
            INSERT INTO stop_reports(task_id, stop_reason, created_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                report.task_id,
                report.stop_reason.value,
                now,
                _model_json(report),
            ),
        )
        self.conn.execute(
            """
            UPDATE tasks
            SET status='stopped', last_stop_reason=?, updated_at=?
            WHERE task_id=?
            """,
            (report.stop_reason.value, now, report.task_id),
        )

    def get_last_stop_report(self, task_id: str) -> StopReport | None:
        row = self.conn.execute(
            """
            SELECT payload_json FROM stop_reports
            WHERE task_id = ?
            ORDER BY report_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return StopReport.model_validate(_loads(str(row["payload_json"])))

    def get_last_stop_reason(self, task_id: str) -> StopReason | None:
        row = self.conn.execute(
            """
            SELECT stop_reason FROM stop_reports
            WHERE task_id = ?
            ORDER BY report_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return StopReason(str(row["stop_reason"])) if row else None

    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot:
        row = self.conn.execute(
            """
            SELECT tokens, cost_usd, llm_calls, tool_calls
            FROM usage_snapshots
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return UsageSnapshot(task_id=task_id)
        return UsageSnapshot(
            task_id=task_id,
            tokens=int(row["tokens"]),
            cost_usd=float(row["cost_usd"]),
            llm_calls=int(row["llm_calls"]),
            tool_calls=int(row["tool_calls"]),
        )

    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> None:
        self._upsert_usage(snapshot)

    def append_event(
        self,
        event_type: EventType,
        payload: dict[str, object],
        *,
        task_id: str | None = None,
        loop_id: int | None = None,
    ) -> None:
        if task_id is not None:
            self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO events(task_id, loop_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, loop_id, event_type.value, json.dumps(payload), _utc_now()),
        )

    def list_events(self, task_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT task_id, loop_id, event_type, payload_json, created_at
            FROM events
            WHERE task_id = ?
            ORDER BY event_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "loop_id": row["loop_id"],
                "event_type": row["event_type"],
                "payload": json.loads(str(row["payload_json"])),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # =====================================================================
    # Section 7 — Memory / Skill
    # =====================================================================
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None:
        self._ensure_task(candidate.task_id)
        self.conn.execute(
            """
            INSERT INTO memory_candidates(
              candidate_id, task_id, status, memory_type, payload_json,
              state, decision_loop_id, decided_by, decision_rationale,
              replaces_candidate_id, expires_at, source_candidate_state_id,
              source_validation_id, source_best_state_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
              status=excluded.status,
              memory_type=excluded.memory_type,
              payload_json=excluded.payload_json,
              state=excluded.state,
              decision_loop_id=excluded.decision_loop_id,
              decided_by=excluded.decided_by,
              decision_rationale=excluded.decision_rationale,
              replaces_candidate_id=excluded.replaces_candidate_id,
              expires_at=excluded.expires_at,
              source_candidate_state_id=excluded.source_candidate_state_id,
              source_validation_id=excluded.source_validation_id,
              source_best_state_id=excluded.source_best_state_id
            """,
            (
                candidate.candidate_id,
                candidate.task_id,
                candidate.status,
                candidate.memory_type,
                _model_json(candidate),
                candidate.state,
                candidate.decision_loop_id,
                candidate.decided_by,
                candidate.decision_rationale,
                candidate.replaces_candidate_id,
                (
                    candidate.expires_at.isoformat().replace("+00:00", "Z")
                    if candidate.expires_at is not None
                    else None
                ),
                candidate.source_candidate_state_id,
                candidate.source_validation_id,
                candidate.source_best_state_id,
            ),
        )

    def list_memory_candidates(self, task_id: str) -> list[MemoryCandidate]:
        rows = self.conn.execute(
            """
            SELECT payload_json FROM memory_candidates
            WHERE task_id = ?
            ORDER BY rowid ASC
            """,
            (task_id,),
        ).fetchall()
        return [
            MemoryCandidate.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    def count_committed_references(self, candidate_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM best_states WHERE state_id = ?",
            (candidate_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def save_skill_card(self, card: SkillCard) -> None:
        self._ensure_task(card.task_id)
        self.conn.execute(
            """
            INSERT INTO skill_cards(skill_id, task_id, name, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
              name=excluded.name,
              payload_json=excluded.payload_json
            """,
            (card.skill_id, card.task_id, card.name, _model_json(card)),
        )

    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]:
        if task_id is None:
            rows = self.conn.execute(
                "SELECT payload_json FROM skill_cards ORDER BY rowid ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT payload_json FROM skill_cards
                WHERE task_id = ?
                ORDER BY rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            SkillCard.model_validate(_loads(str(row["payload_json"])))
            for row in rows
        ]

    # =====================================================================
    # Section 8 — Approvals, misc, transactions, task lock
    # =====================================================================
    def is_approval_granted(self, approval_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM approvals WHERE approval_id = ? LIMIT 1",
            (approval_id,),
        ).fetchone()
        return row is not None

    def reset_no_progress_streak(self, task_id: str) -> None:
        self._ensure_task(task_id)
        self.conn.execute(
            """
            INSERT INTO no_progress_streak(task_id, streak)
            VALUES (?, 0)
            ON CONFLICT(task_id) DO UPDATE SET streak=0
            """,
            (task_id,),
        )

    def increment_no_progress_streak(self, task_id: str) -> int:
        self._ensure_task(task_id)
        with self.transaction():
            current = self.conn.execute(
                "SELECT streak FROM no_progress_streak WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_value = int(current["streak"]) + 1 if current else 1
            self.conn.execute(
                """
                INSERT INTO no_progress_streak(task_id, streak)
                VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET streak=excluded.streak
                """,
                (task_id, next_value),
            )
            return next_value

    def next_loop_id(self, task_id: str) -> int:
        self._ensure_task(task_id)
        rows = [
            self.conn.execute(
                "SELECT COALESCE(MAX(loop_id), 0) AS n FROM loop_traces WHERE task_id=?",
                (task_id,),
            ).fetchone(),
            self.conn.execute(
                "SELECT COALESCE(MAX(loop_id), 0) AS n FROM loop_plans WHERE task_id=?",
                (task_id,),
            ).fetchone(),
            self.conn.execute(
                "SELECT COALESCE(MAX(loop_id), 0) AS n FROM candidates WHERE task_id=?",
                (task_id,),
            ).fetchone(),
        ]
        current = max(int(row["n"]) if row else 0 for row in rows)
        return current + 1

    def acquire_task_lock(
        self,
        task_id: str,
        owner: str,
        *,
        stale_threshold_seconds: int,
        steal: bool = False,
    ) -> Literal["acquired", "reentrant", "held_live", "held_stale", "stolen"]:
        self._ensure_task(task_id)
        now = _utc_now()
        row = self.conn.execute(
            "SELECT owner, locked_at FROM task_locks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO task_locks(task_id, owner, locked_at) VALUES (?, ?, ?)",
                (task_id, owner, now),
            )
            return "acquired"

        existing_owner = str(row["owner"])
        if _same_host_pid(existing_owner, owner):
            return "reentrant"

        locked_at = datetime.fromisoformat(str(row["locked_at"]).replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - locked_at).total_seconds()
        is_stale = elapsed >= stale_threshold_seconds
        if not is_stale and not steal:
            return "held_live"
        if is_stale and not steal:
            return "held_stale"

        prev_locked_at = str(row["locked_at"])
        self.conn.execute(
            "UPDATE task_locks SET owner = ?, locked_at = ? WHERE task_id = ?",
            (owner, now, task_id),
        )
        self.append_event(
            EventType.LOCK_STOLEN,
            {
                "prev_owner": existing_owner,
                "prev_locked_at": prev_locked_at,
                "new_owner": owner,
            },
            task_id=task_id,
        )
        return "stolen"

    def release_task_lock(self, task_id: str, owner: str) -> None:
        self.conn.execute(
            "DELETE FROM task_locks WHERE task_id = ? AND owner = ?",
            (task_id, owner),
        )

    def get_task_lock(self, task_id: str) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT owner, locked_at FROM task_locks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        locked_at = datetime.fromisoformat(
            str(row["locked_at"]).replace("Z", "+00:00")
        )
        return {"owner": str(row["owner"]), "locked_at": locked_at}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self._tx_depth = 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._tx_depth = 0

    # =====================================================================
    # Internal helpers
    # =====================================================================
    def _ensure_task(self, task_id: str) -> None:
        if self.get_task(task_id) is not None:
            return
        now = _utc_now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
              task_id, goal, status, last_stop_reason, created_at, updated_at
            )
            VALUES (?, '', 'pending', NULL, ?, ?)
            """,
            (task_id, now, now),
        )

    def _task_id_for_clock(self, clock: HungerClockState) -> str:
        task_id = self._clock_task_ids.get(id(clock))
        if task_id is not None:
            return task_id
        if self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1:
            row = self.conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()
            return str(row["task_id"])
        raise ValueError("Cannot save HungerClockState without a known task_id")

    def _task_id_for_item(self, item: HungerItem) -> str:
        task_id = self._item_task_ids.get(item.id)
        if task_id is not None:
            return task_id
        rows = self.conn.execute(
            "SELECT task_id FROM hunger_items WHERE item_id = ?",
            (item.id,),
        ).fetchall()
        if len(rows) == 1:
            return str(rows[0]["task_id"])
        raise ValueError(f"Cannot save HungerItem without a known task_id: {item.id}")

    def _save_hunger_item_for_task(self, task_id: str, item: HungerItem) -> None:
        self._ensure_task(task_id)
        self._item_task_ids[item.id] = task_id
        self.conn.execute(
            """
            INSERT INTO hunger_items(
              task_id, item_id, status, gap_score, priority,
              consecutive_failure_count, last_progress_loop_id, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, item_id) DO UPDATE SET
              status=excluded.status,
              gap_score=excluded.gap_score,
              priority=excluded.priority,
              consecutive_failure_count=excluded.consecutive_failure_count,
              last_progress_loop_id=excluded.last_progress_loop_id,
              payload_json=excluded.payload_json
            """,
            (
                task_id,
                item.id,
                item.status.value,
                item.gap_score,
                item.priority,
                item.consecutive_failure_count,
                item.last_progress_loop_id,
                _model_json(item),
            ),
        )

    def _current_loop_id(self, task_id: str) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(MAX(loop_id), 0) AS n
            FROM hunger_snapshots
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return int(row["n"]) + 1 if row else 1

    def _insert_evidence(
        self,
        task_id: str,
        loop_id: int | None,
        evidence_type: EvidenceType,
        payload: dict[str, object],
    ) -> str:
        self._ensure_task(task_id)
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """
            INSERT INTO evidence(evidence_id, task_id, loop_id, evidence_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (eid, task_id, loop_id, evidence_type.value, json.dumps(payload)),
        )
        return eid

    def _upsert_usage(self, usage: UsageSnapshot) -> None:
        self._ensure_task(usage.task_id)
        self.conn.execute(
            """
            INSERT INTO usage_snapshots(task_id, tokens, cost_usd, llm_calls, tool_calls)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              tokens=excluded.tokens,
              cost_usd=excluded.cost_usd,
              llm_calls=excluded.llm_calls,
              tool_calls=excluded.tool_calls
            """,
            (
                usage.task_id,
                usage.tokens,
                usage.cost_usd,
                usage.llm_calls,
                usage.tool_calls,
            ),
        )
