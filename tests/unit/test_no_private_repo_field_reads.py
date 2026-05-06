"""Lint test (PRD §6.2 / D0-11): src/ must not reach into repository
private fields via ``getattr(repo, "_…")`` or ``repo._field`` access.

The protocol-level read paths added in D0-01 cover every access pattern
that previously required reading ``InMemoryRepository._candidates``,
``_task_locks``, ``_accepted_checks``, etc. New code should call those
public methods instead so SQLiteRepository works without source changes.

Allowlist:

* Repository implementations themselves (``src/hungerloop/repository/``)
  obviously read their own state.
* This file is also exempt because it greps the source as data.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "hungerloop"

# Files / dirs that are allowed to reach into private repo storage.
ALLOWLIST_PARTS = {"repository"}

# Patterns that indicate private-field access on a repo.
GETATTR_PRIVATE = re.compile(r'getattr\([^)]*?repo[^,]*,\s*"_[a-z]')
DIRECT_PRIVATE = re.compile(r"\brepo\._[a-z]")
DIRECT_SELF_REPO_PRIVATE = re.compile(r"\bself\.repo\._[a-z]")


def _walk_python_files() -> list[Path]:
    out: list[Path] = []
    for path in SRC.rglob("*.py"):
        if any(part in ALLOWLIST_PARTS for part in path.parts):
            continue
        out.append(path)
    return out


@pytest.mark.parametrize("path", _walk_python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_source_file_has_no_private_repo_field_access(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if GETATTR_PRIVATE.search(line):
            offenders.append(f"{path}:{i}: {line.strip()}")
        elif DIRECT_PRIVATE.search(line):
            offenders.append(f"{path}:{i}: {line.strip()}")
        elif DIRECT_SELF_REPO_PRIVATE.search(line):
            offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, (
        "Private repository fields are not part of the protocol; "
        "use the public method from RepositoryProtocol instead.\n"
        + "\n".join(offenders)
    )
