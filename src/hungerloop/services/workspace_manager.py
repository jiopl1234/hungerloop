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
from typing import Literal

from hungerloop.models.workspace import WorkspaceStatus

_HASH_BUFFER_BYTES = 64 * 1024
_DEFAULT_SEED_IGNORE_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "hungerloop.sqlite*",
        "node_modules",
        "tasks",
    }
)
_CONTINUATION_IGNORE_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "hungerloop.sqlite*",
    }
)


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


def _workspace_file_hashes(root: Path) -> dict[str, str]:
    """Return content hashes for files below a workspace directory."""
    if not root.is_dir():
        return {}
    return {
        relative.as_posix(): _sha256_of_file(path)
        for path in root.rglob("*")
        if not _is_continuation_noise_path(relative := path.relative_to(root))
        if path.is_file()
    }


def _manifest_file_hashes(root: Path) -> dict[str, str] | None:
    manifest_path = root.parent / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    recorded_path = payload.get("path")
    if not isinstance(recorded_path, str):
        return None
    try:
        if Path(recorded_path).resolve() != root.resolve():
            return None
    except OSError:
        return None
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        return None
    hashes: dict[str, str] = {}
    for relative_path, digest in raw_files.items():
        if not isinstance(relative_path, str) or not isinstance(digest, str):
            return None
        if not _is_continuation_noise_path(Path(relative_path)):
            hashes[relative_path] = digest
    return hashes


def _is_continuation_noise_path(path: Path) -> bool:
    for part in path.parts:
        if part in _CONTINUATION_IGNORE_NAMES:
            return True
        if part.startswith("hungerloop.sqlite"):
            return True
    return False


def _copy_seed_source(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"seed source is not a directory: {source}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*sorted(_DEFAULT_SEED_IGNORE_NAMES)),
    )


class WorkspaceManager:
    """Manage per-task ``best/candidate/rejected`` workspace directories."""

    _LINE_COUNT_MAX_BYTES = 262_144

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

    def list_workspace_files(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[str]:
        """Return sorted relative file paths for a task workspace ref."""
        if ref == "best":
            root = self.best_files_dir(task_id)
        else:
            if loop_id is None:
                raise ValueError("loop_id is required for candidate workspace")
            root = self.candidate_files_dir(task_id, loop_id)
        if not root.exists():
            return []

        out: list[str] = []
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if _is_ignored_inventory_path(rel):
                continue
            if path.is_file():
                out.append(rel.as_posix())
        return sorted(out)

    def list_workspace_file_stats(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """Return sorted ``(relative_path, byte_size, line_count)`` tuples.

        ``line_count`` is ``-1`` for binary files and for files larger than
        ``_LINE_COUNT_MAX_BYTES`` (no decode pass on big blobs).
        """
        if ref == "best":
            root = self.best_files_dir(task_id)
        else:
            if loop_id is None:
                raise ValueError("loop_id is required for candidate workspace")
            root = self.candidate_files_dir(task_id, loop_id)
        if not root.exists():
            return []

        out: list[tuple[str, int, int]] = []
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if _is_ignored_inventory_path(rel):
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            lines = -1
            if size <= self._LINE_COUNT_MAX_BYTES:
                try:
                    lines = len(path.read_text(encoding="utf-8").splitlines())
                except (UnicodeDecodeError, OSError):
                    lines = -1
            out.append((rel.as_posix(), size, lines))
        return sorted(out)

    def ensure_task_workspace(self, task_id: str) -> None:
        """Create the ``best/files`` directory if missing."""
        self.best_files_dir(task_id).mkdir(parents=True, exist_ok=True)

    def create_candidate_workspace(
        self,
        task_id: str,
        loop_id: int,
        *,
        seed_source_dir: Path | None = None,
    ) -> Path:
        """Create candidate files from an optional project seed plus ``best``."""
        self.ensure_task_workspace(task_id)

        best = self.best_files_dir(task_id)
        dst = self.candidate_files_dir(task_id, loop_id)

        if dst.exists():
            shutil.rmtree(dst)

        best_has_content = best.exists() and any(best.iterdir())

        if seed_source_dir is not None:
            _copy_seed_source(seed_source_dir, dst)
            if best_has_content:
                shutil.copytree(best, dst, dirs_exist_ok=True)
        elif best_has_content:
            shutil.copytree(best, dst)
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

    def continue_candidate_from_rejected(
        self,
        task_id: str,
        loop_id: int,
        *,
        source_loop_id: int | None = None,
    ) -> bool:
        """Seed a candidate from the immediately prior rejected candidate.

        The rejected tree is copied into the new candidate only. ``best`` and
        the rejected evidence directory are never modified, so I-3/I-4 still
        make the validation/commit gate the sole promotion authority.
        """
        previous_loop_id = (
            loop_id - 1 if source_loop_id is None else source_loop_id
        )
        if previous_loop_id != loop_id - 1:
            raise ValueError("rejected continuation must come from the prior loop")

        source = self.rejected_files_dir(task_id, previous_loop_id)
        if not source.is_dir():
            return False
        best = self.best_files_dir(task_id)
        source_hashes = _manifest_file_hashes(source)
        if source_hashes is None:
            source_hashes = _workspace_file_hashes(source)
        best_hashes = _manifest_file_hashes(best)
        if best_hashes is None:
            best_hashes = _workspace_file_hashes(best)
        if source_hashes == best_hashes:
            return False

        destination = self.candidate_files_dir(task_id, loop_id)
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*sorted(_CONTINUATION_IGNORE_NAMES)),
        )
        self._write_manifest(
            task_id=task_id,
            loop_id=loop_id,
            path=destination,
            status="candidate",
            source_workspace_ref=f"rejected/loop_{previous_loop_id:03d}",
        )
        return True

    def draft_archive_dir(self, task_id: str, loop_id: int, draft_index: int) -> Path:
        return (
            self.task_root(task_id)
            / "candidates"
            / f"loop_{loop_id:03d}"
            / f"draft_{draft_index:02d}"
        )

    def archive_draft(self, task_id: str, loop_id: int, draft_index: int) -> Path:
        """Snapshot the current candidate files tree as ``draft_JJ/``.

        Snapshots live beside ``files/`` inside the loop directory; they are
        audit artifacts and are never validated or promoted directly (I-4:
        only the canonical candidate tree feeds the commit gate).
        """
        source = self.candidate_files_dir(task_id, loop_id)
        destination = self.draft_archive_dir(task_id, loop_id, draft_index)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return destination

    def restore_draft(self, task_id: str, loop_id: int, draft_index: int) -> None:
        """Replace the candidate files tree with an archived draft snapshot."""
        source = self.draft_archive_dir(task_id, loop_id, draft_index)
        if not source.is_dir():
            raise FileNotFoundError(f"draft snapshot not found: {source}")
        destination = self.candidate_files_dir(task_id, loop_id)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        self._write_manifest(
            task_id=task_id,
            loop_id=loop_id,
            path=destination,
            status="candidate",
            source_workspace_ref=(
                f"candidates/loop_{loop_id:03d}/draft_{draft_index:02d}"
            ),
        )

    def candidate_files_hashes(self, task_id: str, loop_id: int) -> dict[str, str]:
        """Content hashes of the current candidate tree (draft short-circuit)."""
        return _workspace_file_hashes(self.candidate_files_dir(task_id, loop_id))

    def workspace_matches_best_manifest(
        self,
        task_id: str,
        workspace_root: Path,
    ) -> bool:
        """Return whether a workspace exactly matches the recorded best manifest."""
        expected_hashes = _manifest_file_hashes(self.best_files_dir(task_id))
        if expected_hashes is None or not workspace_root.is_dir():
            return False
        return _workspace_file_hashes(workspace_root) == expected_hashes

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

    def write_manifest(
        self,
        *,
        task_id: str,
        path: Path,
        status: WorkspaceStatus,
        loop_id: int | None = None,
        source_workspace_ref: str | None = None,
    ) -> None:
        """Write a canonical ``manifest.json`` next to ``path``.

        Public entry point so other services (notably
        :class:`hungerloop.services.repair_state.RepairStateService`)
        can rebuild the manifest after detecting drift without
        diverging from the production write shape.
        """
        self._write_manifest(
            task_id=task_id,
            loop_id=loop_id,
            path=path,
            status=status,
            source_workspace_ref=source_workspace_ref,
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


def _is_ignored_inventory_path(path: Path) -> bool:
    return any(part.startswith(".") or part == "__pycache__" for part in path.parts)
