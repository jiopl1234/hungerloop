"""Filesystem isolation for HungerLoop v0.4.1 candidate/best workspaces.

:class:`WorkspaceManager` implements invariant I-4: every loop iteration runs in
its own candidate directory copied from ``best/``. Successful loops promote the
candidate atomically into ``best/``; failed loops move the candidate into
``rejected/loop_NNN/`` so the agent's bad work never pollutes the committed
tree.

Layout under ``root``::

    tasks/<task_id>/best/files/...           # committed state
    tasks/<task_id>/best/manifest.json
    tasks/<task_id>/candidates/loop_001/files/...
    tasks/<task_id>/candidates/loop_001/manifest.json
    tasks/<task_id>/rejected/loop_002/files/...
    tasks/<task_id>/rejected/loop_002/manifest.json
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from hungerloop.models.workspace import WorkspaceStatus


class WorkspaceManager:
    """Manage per-task ``best/candidate/rejected`` workspace directories."""

    def __init__(self, root: Path) -> None:
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
        """Create the ``best/files`` directory if missing."""
        self.best_files_dir(task_id).mkdir(parents=True, exist_ok=True)

    def create_candidate_workspace(self, task_id: str, loop_id: int) -> Path:
        """Copy ``best/files`` into ``candidates/loop_NNN/files`` and return it."""
        self.ensure_task_workspace(task_id)

        src = self.best_files_dir(task_id)
        dst = self.candidate_files_dir(task_id, loop_id)

        if dst.exists():
            shutil.rmtree(dst)

        if src.exists() and any(src.iterdir()):
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
        """Atomically replace ``best/files`` with the named candidate."""
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
        """Move a candidate workspace into ``rejected/loop_NNN/``."""
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
        status: WorkspaceStatus,
        source_workspace_ref: str | None,
    ) -> None:
        files = [p for p in path.rglob("*") if p.is_file()]
        manifest = {
            "task_id": task_id,
            "loop_id": loop_id,
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
