from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "src" / "hungerloop" / "services"

_ALLOWED_MODULES = {
    "requirement_compiler.py",
    "refinement_compiler.py",
    "hunger_update.py",
    "stagnation_detector.py",
    "handoff_processor.py",
}
_FORBIDDEN_METHODS = {
    "save_hunger_item",
    "save_hunger_ledger",
    "update_hunger_item_status",
}


def _repo_write_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _FORBIDDEN_METHODS:
            continue
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in {"repo", "self"}:
            offenders.append(f"{_display_path(path)}:{node.lineno}:{func.attr}")
        elif isinstance(receiver, ast.Attribute) and receiver.attr == "repo":
            offenders.append(f"{_display_path(path)}:{node.lineno}:{func.attr}")
    return offenders


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def test_no_direct_ledger_writes() -> None:
    offenders = [
        offender
        for path in sorted(SERVICES_DIR.rglob("*.py"))
        if path.name not in _ALLOWED_MODULES
        for offender in _repo_write_offenders(path)
    ]

    assert offenders == []


def test_lint_catches_direct_ledger_write_outside_allowed_module(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_service.py"
    bad_file.write_text(
        "from __future__ import annotations\n\n"
        "class BadService:\n"
        "    def __init__(self, repo):\n"
        "        self.repo = repo\n\n"
        "    def run(self, item):\n"
        "        self.repo.save_hunger_item(item)\n",
        encoding="utf-8",
    )

    assert _repo_write_offenders(bad_file) == [
        f"{bad_file}:8:save_hunger_item"
    ]
