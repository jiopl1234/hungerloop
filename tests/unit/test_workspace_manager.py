"""Atomicity regression tests for WorkspaceManager.

Audit BUG-1: the prior `promote_candidate_to_best` rmtree'd a fixed
`best_backup/` directory on entry, which silently destroyed the only
recoverable copy of best/ after two consecutive promote failures.
These tests pin down the new behavior:

* A failed copytree into staging leaves best/ completely untouched.
* A failed rename during the swap rolls back to the prior best/.
* Two consecutive failures don't compound into data loss.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hungerloop.services.workspace_manager import WorkspaceManager


def _seed(tmp_path: Path) -> tuple[WorkspaceManager, Path, Path]:
    wm = WorkspaceManager(tmp_path)
    wm.ensure_task_workspace("t1")
    best = wm.best_files_dir("t1")
    (best / "original.txt").write_text("ORIGINAL")
    wm.create_candidate_workspace("t1", 1)
    cand = wm.candidate_files_dir("t1", 1)
    (cand / "new.txt").write_text("NEW")
    return wm, best, cand


def test_promote_succeeds_moves_candidate_content_into_best(
    tmp_path: Path,
) -> None:
    """Happy path: promote replaces best/ with the candidate.

    Note: candidate workspaces start as copies of best/ (I-4), so
    `original.txt` is *also* in the candidate and persists through
    the promote — what's important is that the new file landed.
    """
    wm, best, _ = _seed(tmp_path)
    wm.promote_candidate_to_best("t1", loop_id=1)
    assert (best / "new.txt").read_text() == "NEW"
    # original.txt persists because the candidate was forked from best.
    assert (best / "original.txt").read_text() == "ORIGINAL"


def test_promote_copytree_failure_leaves_best_intact(tmp_path: Path) -> None:
    """Atomicity rule #1: a failure during the staging copy must not
    touch the live best/ — the user's prior good state stays in place."""
    wm, best, _ = _seed(tmp_path)

    with patch(
        "hungerloop.services.workspace_manager.shutil.copytree",
        side_effect=OSError("simulated disk full"),
    ):
        with pytest.raises(OSError, match="simulated"):
            wm.promote_candidate_to_best("t1", loop_id=1)

    # best/ must still exist with original content.
    assert best.exists()
    assert (best / "original.txt").read_text() == "ORIGINAL"


def test_promote_rename_failure_rolls_back_to_prior_best(
    tmp_path: Path,
) -> None:
    """Atomicity rule #2: if the second rename (staging -> best) fails,
    we must rename the saved old/ back to best/."""
    wm, best, _ = _seed(tmp_path)

    real_rename = __import__("os").rename
    call_count = {"n": 0}

    def flaky_rename(src: str, dst: str) -> None:
        call_count["n"] += 1
        # Allow the first rename (best -> .best.old.*) but fail the
        # second (.best.staging.* -> best).
        if call_count["n"] == 2:
            raise OSError("simulated rename failure")
        real_rename(src, dst)

    with patch(
        "hungerloop.services.workspace_manager.os.rename",
        side_effect=flaky_rename,
    ):
        with pytest.raises(OSError, match="simulated rename"):
            wm.promote_candidate_to_best("t1", loop_id=1)

    # Recovery: best/ exists with original content thanks to the rollback.
    assert best.exists()
    assert (best / "original.txt").read_text() == "ORIGINAL"


def test_two_consecutive_failures_do_not_destroy_original(
    tmp_path: Path,
) -> None:
    """BUG-1 regression: two consecutive promote failures must not
    compound into data loss. The old fixed `best_backup/` that the
    function rmtree'd on entry silently destroyed the only good copy.
    The new sibling-token model gives each call its own recovery
    directory so retries can't clobber each other.
    """
    wm, best, _ = _seed(tmp_path)
    wm.create_candidate_workspace("t1", 2)
    cand2 = wm.candidate_files_dir("t1", 2)
    (cand2 / "newer.txt").write_text("NEWER")

    # First promote: copytree fails. best/ must stay intact.
    with patch(
        "hungerloop.services.workspace_manager.shutil.copytree",
        side_effect=OSError("disk full #1"),
    ):
        with pytest.raises(OSError, match="disk full #1"):
            wm.promote_candidate_to_best("t1", loop_id=1)

    assert (best / "original.txt").read_text() == "ORIGINAL"

    # Second promote: copytree fails again. best/ must STILL be intact.
    with patch(
        "hungerloop.services.workspace_manager.shutil.copytree",
        side_effect=OSError("disk full #2"),
    ):
        with pytest.raises(OSError, match="disk full #2"):
            wm.promote_candidate_to_best("t1", loop_id=2)

    assert (best / "original.txt").read_text() == "ORIGINAL", (
        "BUG-1 regression: two consecutive failures destroyed original "
        "content. Each promote must use a unique recovery directory; "
        "see workspace_manager.promote_candidate_to_best."
    )


def test_promote_with_no_prior_best_uses_no_rollback_path(
    tmp_path: Path,
) -> None:
    """Edge: empty best/ (no original.txt). Staging + single rename suffices."""
    wm = WorkspaceManager(tmp_path)
    wm.ensure_task_workspace("t1")
    # best/ exists but is empty
    wm.create_candidate_workspace("t1", 1)
    (wm.candidate_files_dir("t1", 1) / "fresh.txt").write_text("FRESH")

    wm.promote_candidate_to_best("t1", loop_id=1)
    assert (wm.best_files_dir("t1") / "fresh.txt").read_text() == "FRESH"


def test_promote_cleans_up_staging_directory(tmp_path: Path) -> None:
    """No .best.staging.* or .best.old.* should leak after a successful
    promote."""
    wm, best, _ = _seed(tmp_path)
    wm.promote_candidate_to_best("t1", loop_id=1)

    leftovers = [
        p
        for p in best.parent.iterdir()
        if p.name.startswith(".best.staging.") or p.name.startswith(".best.old.")
    ]
    assert leftovers == [], f"staging/old dirs leaked: {leftovers}"


def test_promote_missing_candidate_raises(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    wm.ensure_task_workspace("t1")
    with pytest.raises(FileNotFoundError, match="Candidate workspace"):
        wm.promote_candidate_to_best("t1", loop_id=42)
