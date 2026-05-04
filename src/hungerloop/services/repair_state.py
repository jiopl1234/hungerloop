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
from typing import Any, Literal

from pydantic import BaseModel

from hungerloop.models.events import EventType
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.workspace_manager import WorkspaceManager, _sha256_of_file

DivergenceKind = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7"]


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

        Empty list = clean (D1 across the board). Order is stable: D2 →
        D3 → D4 → D5 → D6 → D7 so the CLI's stdout summary is easy to
        diff between runs.
        """
        divergences: list[Divergence] = []
        divergences.extend(self._detect_best_manifest(task_id))
        divergences.extend(self._detect_orphan_candidates(task_id))
        divergences.extend(self._detect_stale_lock(task_id))
        divergences.extend(self._detect_dangling_accepted_checks(task_id))
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
        candidates_attr: dict[str, Any] = getattr(self.repo, "_candidates", {}) or {}
        candidate_loop_ids: set[int] = set()
        for cand in candidates_attr.values():
            cand_task = getattr(cand, "task_id", None)
            cand_loop = getattr(cand, "loop_id", None)
            if cand_task == task_id and isinstance(cand_loop, int):
                candidate_loop_ids.add(cand_loop)

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
        locks: dict[str, dict[str, Any]] = getattr(self.repo, "_task_locks", {}) or {}
        info = locks.get(task_id)
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
        accepted: dict[tuple[str, str], dict[str, Any]] = getattr(
            self.repo, "_accepted_checks", {}
        ) or {}
        validations: dict[str, Any] = getattr(
            self.repo, "_validation_reports", {}
        ) or {}

        divergences: list[Divergence] = []
        for (rec_task, check_key), record in sorted(accepted.items()):
            if rec_task != task_id:
                continue
            validation_id = record.get("validation_id")
            if validation_id is None:
                continue
            if validation_id not in validations:
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
        # D1/D2/D3 are not user-fixable (D1 is clean; D2/D3 returned above).
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
