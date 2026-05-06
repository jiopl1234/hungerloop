"""Workspace ↔ repository divergence detection (PRD §16.3).

``RepairStateService`` powers ``hungerloop repair-state`` (b0-03):

* :meth:`detect` is read-only — it walks the workspace and the repository
  and emits one :class:`Divergence` row per anomaly. It never deletes a
  file and never overwrites ``best/``.
* :meth:`apply_fix` repairs a single divergence when policy allows: today
  that means rewriting a missing manifest (D4) and moving an orphan
  candidate to ``rejected/`` (D5). Corruption (D2/D3) refuses by design;
  stale locks (D6) and dangling validation rows (D7) refuse for v0.5b.0
  and leave the operator a remediation path.

Filesystem is the source of truth; SQLite is an auxiliary index. That
asymmetry is the whole reason ``repair-state`` exists: when the index
disagrees with the disk, we trust the disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from hungerloop.models.enums import StopReason
from hungerloop.models.events import EventType
from hungerloop.models.tracing import StopReport
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.workspace_manager import WorkspaceManager, _sha256_of_file

DivergenceKind = Literal[
    "D1",
    "D2",
    "D3",
    "D4",
    "D5",
    "D6",
    "D7",
    "D8",
    "D9",
    "D10",
    "D11",
    "D12",
    "D13",
]


class Divergence(BaseModel):
    """One row of detected drift between filesystem and SQLite/state.

    ``corruption`` is the policy hinge: ``apply_fix`` refuses to act on a
    divergence with ``corruption=True``; the operator must restore from a
    backup. D6 (stale lock) and D7 (missing validation) are non-corruption
    refusals that point the operator at a specific remediation.
    """

    kind: DivergenceKind
    target: str
    detail: str
    corruption: bool


@dataclass
class FixOutcome:
    """Result of applying one fix; returned to the CLI as ``(fixed, summary)``."""

    fixed: bool
    summary: str


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Mirrors ``run_cmd.DEFAULT_LOCK_STALE_SEC`` so users see the same threshold
# whether they're acquiring a lock or asking the repair tool whether one
# went stale (decision §11.3).
DEFAULT_STALE_THRESHOLD_SECONDS = 30 * 60


class RepairStateService:
    """Detect and (optionally) repair workspace ↔ repo divergence."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        workspace_root: Path,
        stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
        clock: datetime | None = None,
    ) -> None:
        self.repo = repo
        self.workspace_root = Path(workspace_root)
        self.stale_threshold_seconds = stale_threshold_seconds
        # ``clock`` is plumbing for tests: pinning ``now`` lets D6 tests
        # land deterministically without monkey-patching ``datetime``.
        self._clock_override = clock
        self._workspace = WorkspaceManager(self.workspace_root)

    # -----------------------------------------------------------------
    # Detection
    # -----------------------------------------------------------------
    def detect(self, task_id: str) -> list[Divergence]:
        """Return every divergence row we find for ``task_id``.

        Empty list = clean (D1 across the board). Order is stable so the
        CLI's stdout summary is easy to diff between runs:

        D2 → D3 → D4 (manifest) → D5 (orphan candidates) → D6 (stale
        lock) → D7 (dangling validation, deferred fix) → D8 (accepted
        checks → missing validation, corruption) → D9 (best_state →
        missing validation, corruption) → D10 (stopped task with no
        StopReport) → D11 (usage snapshot drift) → D12 (workspace ↔
        candidate row mismatch) → D13 (event references missing trace).
        """
        divergences: list[Divergence] = []
        divergences.extend(self._detect_best_manifest(task_id))
        divergences.extend(self._detect_orphan_candidates(task_id))
        divergences.extend(self._detect_stale_lock(task_id))
        divergences.extend(self._detect_dangling_accepted_checks(task_id))
        divergences.extend(self._detect_d8_accepted_missing_validation(task_id))
        divergences.extend(self._detect_d9_best_state_missing_validation(task_id))
        divergences.extend(self._detect_d10_stopped_no_report(task_id))
        divergences.extend(self._detect_d11_usage_drift(task_id))
        divergences.extend(self._detect_d12_candidate_workspace_mismatch(task_id))
        divergences.extend(self._detect_d13_event_orphans(task_id))
        return divergences

    def _detect_best_manifest(self, task_id: str) -> list[Divergence]:
        """D2/D3/D4 — manifest ↔ filesystem reconciliation for ``best/``."""
        best_dir = self._workspace.best_files_dir(task_id)
        manifest_path = best_dir.parent / "manifest.json"

        # No best/ at all: nothing to reconcile (a brand-new task).
        if not best_dir.exists():
            return []

        files_on_disk = sorted(p for p in best_dir.rglob("*") if p.is_file())
        on_disk_relative = {p.relative_to(best_dir).as_posix() for p in files_on_disk}

        # D4: files exist but the manifest is missing entirely.
        if not manifest_path.exists():
            if on_disk_relative:
                return [
                    Divergence(
                        kind="D4",
                        target=str(best_dir),
                        detail=(
                            f"{len(on_disk_relative)} file(s) under best/ but "
                            "no manifest.json"
                        ),
                        corruption=False,
                    )
                ]
            return []

        try:
            manifest_blob = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            return [
                Divergence(
                    kind="D2",
                    target=str(manifest_path),
                    detail=f"manifest unreadable: {exc!r}",
                    corruption=True,
                )
            ]

        recorded_hashes = manifest_blob.get("files") or {}
        if not isinstance(recorded_hashes, dict):
            return [
                Divergence(
                    kind="D2",
                    target=str(manifest_path),
                    detail="manifest.files is not a dict",
                    corruption=True,
                )
            ]

        # D4 also covers the "manifest exists but has no hashes" case —
        # that's the legacy manifest format from before this commit.
        if not recorded_hashes and on_disk_relative:
            return [
                Divergence(
                    kind="D4",
                    target=str(best_dir),
                    detail=(
                        "manifest.json present but lacks per-file hashes "
                        "(pre-v0.5b.0 manifest)"
                    ),
                    corruption=False,
                )
            ]

        divergences: list[Divergence] = []
        recorded_keys = set(recorded_hashes.keys())

        # D3: manifest references files that aren't on disk anymore.
        for missing in sorted(recorded_keys - on_disk_relative):
            divergences.append(
                Divergence(
                    kind="D3",
                    target=str(best_dir / missing),
                    detail="file referenced by manifest is missing on disk",
                    corruption=True,
                )
            )

        # D2: same path on disk and in manifest, but content differs.
        for shared in sorted(recorded_keys & on_disk_relative):
            actual = _sha256_of_file(best_dir / shared)
            recorded = recorded_hashes[shared]
            if actual != recorded:
                divergences.append(
                    Divergence(
                        kind="D2",
                        target=str(best_dir / shared),
                        detail=(
                            f"sha256 mismatch (manifest={recorded[:8]}…, "
                            f"actual={actual[:8]}…)"
                        ),
                        corruption=True,
                    )
                )

        # D4 (subcase): files present on disk that the manifest doesn't
        # describe. Treated as drift, not corruption — the file is real,
        # the index is just stale.
        for extra in sorted(on_disk_relative - recorded_keys):
            divergences.append(
                Divergence(
                    kind="D4",
                    target=str(best_dir / extra),
                    detail="file on disk has no manifest entry",
                    corruption=False,
                )
            )

        return divergences

    def _detect_orphan_candidates(self, task_id: str) -> list[Divergence]:
        """D5 — candidate workspace dirs with no LoopTrace + no candidate row."""
        candidates_root = self._workspace.task_root(task_id) / "candidates"
        if not candidates_root.exists():
            return []

        traces = self.repo.list_loop_traces(task_id)
        loop_ids_with_traces = {t.loop_id for t in traces}
        candidate_loop_ids: set[int] = {
            cand.loop_id for cand in self.repo.list_candidates_for_task(task_id)
        }

        divergences: list[Divergence] = []
        for entry in sorted(candidates_root.iterdir()):
            if not entry.is_dir():
                continue
            loop_id = _parse_loop_dir(entry.name)
            if loop_id is None:
                continue
            if (
                loop_id not in loop_ids_with_traces
                and loop_id not in candidate_loop_ids
            ):
                divergences.append(
                    Divergence(
                        kind="D5",
                        target=str(entry),
                        detail=(
                            f"candidates/loop_{loop_id:03d} has no LoopTrace "
                            "and no candidate row"
                        ),
                        corruption=False,
                    )
                )
        return divergences

    def _detect_stale_lock(self, task_id: str) -> list[Divergence]:
        """D6 — task lock present and older than ``stale_threshold_seconds``."""
        info = self.repo.get_task_lock(task_id)
        if not info:
            return []
        locked_at = info.get("locked_at")
        if not isinstance(locked_at, datetime):
            return []
        now = self._now()
        if locked_at.tzinfo is None:
            locked_at = locked_at.replace(tzinfo=timezone.utc)
        age = (now - locked_at).total_seconds()
        if age <= self.stale_threshold_seconds:
            return []
        return [
            Divergence(
                kind="D6",
                target=task_id,
                detail=(
                    f"task lock owned by {info.get('owner')!r} for "
                    f"{int(age)}s (threshold {self.stale_threshold_seconds}s)"
                ),
                corruption=False,
            )
        ]

    def _detect_dangling_accepted_checks(self, task_id: str) -> list[Divergence]:
        """D7 — ``accepted_checks`` row references a missing validation_id.

        Detection lands in v0.5b.0 so operators can see the issue; the fix
        is deferred to v0.5b.1.
        """
        divergences: list[Divergence] = []
        for record in self.repo.iter_accepted_checks(task_id):
            validation_id = record.get("validation_id")
            check_key = record.get("check_key")
            if not isinstance(validation_id, str) or not isinstance(check_key, str):
                continue
            if not self.repo.validation_exists(validation_id):
                divergences.append(
                    Divergence(
                        kind="D7",
                        target=f"{task_id}:{check_key}",
                        detail=(
                            f"accepted_checks references missing "
                            f"validation_id={validation_id!r}"
                        ),
                        corruption=False,
                    )
                )
        return divergences

    def _detect_d8_accepted_missing_validation(
        self, task_id: str
    ) -> list[Divergence]:
        """D8 — same predicate as D7, but tagged corruption.

        D7 was the v0.5b.0 detection-only flavour (warning-only); D8
        re-classifies the *exact same* dangling row as corruption per
        PRD §13 (FR-9) so ``--check`` exits 2 and operators are forced
        to restore from backup or re-run validation. Both rows fire on
        the same divergence so v0.5b.1 audit trails surface either form
        depending on the operator's --apply path.
        """
        divergences: list[Divergence] = []
        for record in self.repo.iter_accepted_checks(task_id):
            validation_id = record.get("validation_id")
            check_key = record.get("check_key")
            if not isinstance(validation_id, str) or not isinstance(check_key, str):
                continue
            if not self.repo.validation_exists(validation_id):
                divergences.append(
                    Divergence(
                        kind="D8",
                        target=f"{task_id}:{check_key}",
                        detail=(
                            f"accepted_checks references missing "
                            f"validation_id={validation_id!r} (corruption)"
                        ),
                        corruption=True,
                    )
                )
        return divergences

    def _detect_d9_best_state_missing_validation(
        self, task_id: str
    ) -> list[Divergence]:
        """D9 — ``best_state.validation_id`` references a missing validation row."""
        best = self.repo.get_best_state(task_id)
        if best is None or best.validation_id is None:
            return []
        if self.repo.validation_exists(best.validation_id):
            return []
        return [
            Divergence(
                kind="D9",
                target=f"{task_id}:{best.state_id}",
                detail=(
                    f"best_state.validation_id={best.validation_id!r} not "
                    "present in validation_reports"
                ),
                corruption=True,
            )
        ]

    def _detect_d10_stopped_no_report(self, task_id: str) -> list[Divergence]:
        """D10 — task row marked stopped but no StopReport persisted."""
        task = self.repo.get_task(task_id)
        if task is None or task.status != "stopped":
            return []
        if self.repo.get_last_stop_report(task_id) is not None:
            return []
        return [
            Divergence(
                kind="D10",
                target=task_id,
                detail=(
                    "task.status='stopped' but no StopReport row exists; "
                    "auto-recoverable via --apply"
                ),
                corruption=False,
            )
        ]

    def _detect_d11_usage_drift(self, task_id: str) -> list[Divergence]:
        """D11 — ``usage_snapshot`` differs from sum of evidence rows by >5%
        on either tokens or cost (whichever is larger)."""
        recomputed = self.repo.aggregate_evidence_usage(task_id)
        snapshot = self.repo.get_usage_snapshot(task_id)
        # Missing snapshot when evidence exists is a divergence; but a
        # truly-empty task (no evidence rows, no snapshot) is clean.
        if (
            recomputed.tokens == 0
            and recomputed.cost_usd == 0.0
            and recomputed.llm_calls == 0
            and recomputed.tool_calls == 0
            and snapshot.tokens == 0
            and snapshot.cost_usd == 0.0
            and snapshot.llm_calls == 0
            and snapshot.tool_calls == 0
        ):
            return []

        tokens_drift = _ratio(snapshot.tokens, recomputed.tokens)
        cost_drift = _ratio(snapshot.cost_usd, recomputed.cost_usd)
        worst = max(tokens_drift, cost_drift)
        if worst <= 0.05:
            return []
        return [
            Divergence(
                kind="D11",
                target=task_id,
                detail=(
                    f"usage_snapshot drifted from evidence sum by "
                    f"{worst * 100:.1f}% "
                    f"(snapshot: tokens={snapshot.tokens}, "
                    f"cost=${snapshot.cost_usd:.4f}; "
                    f"evidence: tokens={recomputed.tokens}, "
                    f"cost=${recomputed.cost_usd:.4f})"
                ),
                corruption=False,
            )
        ]

    def _detect_d12_candidate_workspace_mismatch(
        self, task_id: str
    ) -> list[Divergence]:
        """D12 — candidate workspace dir exists but row marks loop terminal."""
        candidates_root = self._workspace.task_root(task_id) / "candidates"
        if not candidates_root.exists():
            return []
        terminal_ids: set[int] = set()
        for cand in self.repo.list_candidates_for_task(task_id):
            if self.repo.candidate_status(cand.id) in {"committed", "rejected"}:
                terminal_ids.add(cand.loop_id)

        divergences: list[Divergence] = []
        for entry in sorted(candidates_root.iterdir()):
            if not entry.is_dir():
                continue
            loop_id = _parse_loop_dir(entry.name)
            if loop_id is None or loop_id not in terminal_ids:
                continue
            divergences.append(
                Divergence(
                    kind="D12",
                    target=str(entry),
                    detail=(
                        f"candidate row for loop_{loop_id:03d} is marked "
                        "committed/rejected but the candidate workspace "
                        "still lives in candidates/"
                    ),
                    corruption=False,
                )
            )
        return divergences

    def _detect_d13_event_orphans(self, task_id: str) -> list[Divergence]:
        """D13 — event references a loop_id with no LoopTrace row."""
        events = self.repo.list_events(task_id)
        seen_loops: set[int] = set()
        divergences: list[Divergence] = []
        for ev in events:
            loop_id = ev.get("loop_id")
            if not isinstance(loop_id, int) or loop_id in seen_loops:
                continue
            if self.repo.get_loop_trace(task_id, loop_id) is None:
                divergences.append(
                    Divergence(
                        kind="D13",
                        target=f"{task_id}:loop_{loop_id:03d}",
                        detail=(
                            f"event log references loop_id={loop_id} "
                            "but no LoopTrace row exists"
                        ),
                        corruption=False,
                    )
                )
            seen_loops.add(loop_id)
        return divergences

    # -----------------------------------------------------------------
    # Repair
    # -----------------------------------------------------------------
    def apply_fix(self, divergence: Divergence) -> FixOutcome:
        """Apply the (limited) v0.5b.0 repair set; refuse otherwise.

        Every call writes a ``repair_state_action`` event so the audit
        trail records refusals as well as successful repairs.
        """
        if divergence.corruption:
            return self._refuse(
                divergence,
                summary=(
                    "refusing to fix corruption; restore from backup "
                    "(see <db>.bak.v* siblings)"
                ),
            )

        if divergence.kind == "D4":
            return self._fix_d4(divergence)
        if divergence.kind == "D5":
            return self._fix_d5(divergence)
        if divergence.kind == "D6":
            return self._refuse(
                divergence,
                summary=(
                    "stale lock not auto-cleared; rerun with "
                    "'hungerloop run --steal-lock'"
                ),
            )
        if divergence.kind == "D7":
            return self._refuse(
                divergence,
                summary=(
                    "deferred to v0.5b.1; for now, re-run validation "
                    "or restore from backup"
                ),
            )
        if divergence.kind == "D10":
            return self._fix_d10(divergence)
        if divergence.kind == "D11":
            return self._fix_d11(divergence)
        if divergence.kind in {"D12", "D13"}:
            return self._refuse(
                divergence,
                summary=(
                    f"{divergence.kind} is detection-only; restore from "
                    "backup or re-run validation manually"
                ),
            )
        # D1/D2/D3/D8/D9 are not user-fixable (D1 is clean; the rest are
        # corruption that returned above before this dispatch).
        return self._refuse(divergence, summary="no fix path for this divergence")

    def _fix_d4(self, divergence: Divergence) -> FixOutcome:
        """Rebuild the ``best/`` manifest from the live filesystem.

        Routes through ``WorkspaceManager.write_manifest`` so the JSON
        shape matches the production write path exactly. Pre-review the
        repair path emitted a Pydantic ``model_dump`` shape that carried
        extra fields (``workspace_ref``, ``artifact_ids``, ...) that
        ``_write_manifest`` never writes — operators inspecting the file
        would see schema drift between the two code paths.
        """
        target_path = Path(divergence.target)
        task_id = self._task_id_from_path(target_path)
        if task_id is None:
            return self._refuse(
                divergence,
                summary=f"could not infer task_id from {target_path}",
            )

        best_dir = self._workspace.best_files_dir(task_id)
        if not best_dir.exists():
            return self._refuse(
                divergence, summary=f"best/ does not exist for task {task_id}"
            )

        self._workspace.write_manifest(
            task_id=task_id,
            path=best_dir,
            status="best",
        )

        files_on_disk = [p for p in best_dir.rglob("*") if p.is_file()]
        total_bytes = sum(p.stat().st_size for p in files_on_disk)
        summary = (
            f"rebuilt manifest for {best_dir} "
            f"({len(files_on_disk)} file(s), {total_bytes} bytes)"
        )
        self._emit_event(
            task_id=task_id,
            kind="D4",
            action="fix",
            target=str(best_dir),
            summary=summary,
        )
        return FixOutcome(fixed=True, summary=summary)

    def _fix_d5(self, divergence: Divergence) -> FixOutcome:
        """Move the orphan candidate workspace to ``rejected/``."""
        candidate_path = Path(divergence.target)
        task_id = self._task_id_from_path(candidate_path)
        loop_id = _parse_loop_dir(candidate_path.name)
        if task_id is None or loop_id is None:
            return self._refuse(
                divergence,
                summary=(
                    f"could not infer (task_id, loop_id) from {candidate_path}"
                ),
            )
        # ``WorkspaceManager.reject_candidate`` expects the ``files/`` dir
        # to exist; if the candidate dir was created without ``files/``
        # (e.g. a hand-rolled stub) move the whole tree manually and write
        # the manifest with the same canonical shape so a follow-up
        # --check doesn't see an empty rejected/ dir as a new D4.
        files_dir = candidate_path / "files"
        if files_dir.exists():
            self._workspace.reject_candidate(task_id, loop_id)
        else:
            rejected = (
                self._workspace.task_root(task_id)
                / "rejected"
                / candidate_path.name
            )
            rejected.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.rename(rejected)
            self._workspace.write_manifest(
                task_id=task_id,
                path=rejected,
                loop_id=loop_id,
                status="rejected",
                source_workspace_ref=f"candidates/loop_{loop_id:03d}",
            )
        summary = (
            f"moved orphan candidate loop_{loop_id:03d} to rejected/"
        )
        self._emit_event(
            task_id=task_id,
            kind="D5",
            action="fix",
            target=str(candidate_path),
            summary=summary,
        )
        return FixOutcome(fixed=True, summary=summary)

    def _fix_d10(self, divergence: Divergence) -> FixOutcome:
        """Synthesise a StopReport from the latest LoopTrace (PRD §13 / FR-12)."""
        task_id = divergence.target
        traces = self.repo.list_loop_traces(task_id)
        if not traces:
            return self._refuse(
                divergence,
                summary=(
                    f"no LoopTrace rows for task {task_id}; cannot synthesise "
                    "a StopReport — restore from backup or re-run the task"
                ),
            )
        latest = traces[-1]
        report = StopReport(
            task_id=task_id,
            stop_reason=StopReason.ERROR,
            goal_status="abandoned",
            last_loop_id=latest.loop_id,
            recommendation="auto-recovered by repair-state --apply (D10)",
        )
        self.repo.save_stop_report(report)
        summary = (
            f"synthesised StopReport(stop_reason=ERROR) for task {task_id} "
            f"from loop {latest.loop_id} trace"
        )
        self._emit_event(
            task_id=task_id,
            kind="D10",
            action="fix",
            target=task_id,
            summary=summary,
        )
        return FixOutcome(fixed=True, summary=summary)

    def _fix_d11(self, divergence: Divergence) -> FixOutcome:
        """Recompute usage_snapshot from evidence rows; persist (PRD §13 / FR-12)."""
        task_id = divergence.target
        recomputed = self.repo.aggregate_evidence_usage(task_id)
        self.repo.save_usage_snapshot(recomputed)
        summary = (
            f"rewrote usage_snapshot for task {task_id} from evidence "
            f"(tokens={recomputed.tokens}, cost=${recomputed.cost_usd:.4f}, "
            f"llm_calls={recomputed.llm_calls}, "
            f"tool_calls={recomputed.tool_calls})"
        )
        self._emit_event(
            task_id=task_id,
            kind="D11",
            action="fix",
            target=task_id,
            summary=summary,
        )
        return FixOutcome(fixed=True, summary=summary)

    def _refuse(
        self, divergence: Divergence, *, summary: str
    ) -> FixOutcome:
        # Map back to a task_id for the audit row when we can.
        task_id = self._task_id_from_path(Path(divergence.target))
        self._emit_event(
            task_id=task_id,
            kind=divergence.kind,
            action="refuse",
            target=divergence.target,
            summary=summary,
        )
        return FixOutcome(fixed=False, summary=summary)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock_override or datetime.now(timezone.utc)

    def _task_id_from_path(self, path: Path) -> str | None:
        """Pull ``task_id`` out of a path under ``<root>/tasks/<id>/...``."""
        try:
            rel = path.resolve().relative_to(self.workspace_root.resolve())
        except (ValueError, OSError):
            return None
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "tasks":
            return parts[1]
        return None

    def _emit_event(
        self,
        *,
        task_id: str | None,
        kind: DivergenceKind,
        action: Literal["fix", "refuse"],
        target: str,
        summary: str,
    ) -> None:
        self.repo.append_event(
            EventType.REPAIR_STATE_ACTION,
            {
                "kind": kind,
                "action": action,
                "target": target,
                "summary": summary,
            },
            task_id=task_id,
        )


def _parse_loop_dir(name: str) -> int | None:
    """Return the loop id parsed from ``loop_NNN`` or ``None``."""
    if not name.startswith("loop_"):
        return None
    suffix = name[len("loop_"):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def _ratio(actual: float | int, expected: float | int) -> float:
    """Return the absolute relative drift between two scalars.

    ``ratio(0, 0) == 0`` (no drift on a fully-empty pair); when
    ``expected`` is zero but ``actual`` is not, the divergence is
    treated as 100% (``1.0``) so the D11 detector flags an orphan
    snapshot. Otherwise ``abs(actual - expected) / abs(expected)``.
    """
    if expected == 0 and actual == 0:
        return 0.0
    if expected == 0:
        return 1.0
    return abs(float(actual) - float(expected)) / abs(float(expected))
