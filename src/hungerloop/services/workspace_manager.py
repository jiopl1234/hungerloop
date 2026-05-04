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

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hungerloop.models.workspace import WorkspaceStatus

_HASH_BUFFER_BYTES = 64 * 1024


def _sha256_of_file(path: Path) -> str:
    """Return the lowercase hex sha256 digest of ``path``.

    Streamed in 64 KiB blocks so we don't load multi-MB workspace files
    into memory; ``repair-state --check`` calls this once per file in
    ``best/files/``.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
        """Atomically replace ``best/files`` with the named candidate.

        Atomicity model
        ---------------
        We rely on POSIX ``rename(2)``: a rename within one filesystem is
        atomic. We never call ``shutil.copytree`` *into* the live ``best/``
        path, because a mid-copy failure would leave ``best/`` partially
        written with no good backup.

        Sequence:

        1. Copy the candidate into a sibling staging directory
           ``best/../.best.staging.<unique>/``. If this fails, ``best/``
           is untouched and we raise — the caller's prior good state is
           still in place.
        2. Rename the live ``best/`` to a sibling
           ``best/../.best.old.<unique>/`` (atomic on POSIX).
        3. Rename the staging directory to ``best/`` (atomic on POSIX).
           If this rename fails, we rename the ``.best.old.*`` back to
           ``best/`` so the caller is left with the prior good state.
        4. Remove the ``.best.old.*`` directory.

        The ``<unique>`` suffix uses ``os.getpid() + uuid4`` so two
        concurrent failed promotes can't clobber each other's recovery
        data — the previous implementation used a fixed ``best_backup/``
        name and *deleted it on entry*, which silently destroyed
        recoverable data after a second consecutive failure (BUG-1 in
        the test audit).

        Raises:
            FileNotFoundError: Candidate workspace doesn't exist.
            OSError: Filesystem error during copy/rename. Best/ left
                unchanged in this case (atomicity guarantee).
        """
        candidate = self.candidate_files_dir(task_id, loop_id)
        best = self.best_files_dir(task_id)

        if not candidate.exists():
            raise FileNotFoundError(f"Candidate workspace not found: {candidate}")

        token = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
        staging = best.parent / f".best.staging.{token}"
        old = best.parent / f".best.old.{token}"

        # 1. Stage the new content. If this fails, best/ untouched.
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(candidate, staging)

        try:
            # 2-3. Atomic swap.
            if best.exists():
                os.rename(str(best), str(old))
                try:
                    os.rename(str(staging), str(best))
                except Exception:
                    # Roll back: put the old best/ back in place.
                    os.rename(str(old), str(best))
                    raise
            else:
                # No prior best/; just rename staging into place.
                os.rename(str(staging), str(best))
        except Exception:
            # Clean up staging if it's still around (atomicity preserved).
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

        # 4. Remove the displaced old directory (after the swap succeeded).
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)

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
        # Hash every file under ``path`` so ``repair-state`` can detect
        # filesystem drift (PRD §16.3 D1/D2/D3). Keys are POSIX-style
        # relative paths so manifests round-trip across platforms.
        file_hashes: dict[str, str] = {
            p.relative_to(path).as_posix(): _sha256_of_file(p) for p in files
        }
        manifest = {
            "task_id": task_id,
            "loop_id": loop_id,
            "path": str(path),
            "source_workspace_ref": source_workspace_ref,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "total_bytes": sum(p.stat().st_size for p in files),
            "files": file_hashes,
        }
        (path.parent / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
