from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "hungerloop"
ALLOWED_WRITER = Path("src/hungerloop/services/hunger_engine.py")
ALLOWED_REPOSITORY_FILES = {
    Path("src/hungerloop/repository/protocol.py"),
    Path("src/hungerloop/repository/in_memory_repo.py"),
    Path("src/hungerloop/repository/sqlite_repo.py"),
}


def _phase_status_write_offenders(path: Path) -> list[str]:
    relative = _display_path(path)
    if relative in ALLOWED_REPOSITORY_FILES:
        return []

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if _is_update_phase_status_call(node):
            function = _enclosing_function(tree, node)
            if not (relative == ALLOWED_WRITER and function == "_transition_phase"):
                offenders.append(f"{relative}:{node.lineno}")
        elif _is_raw_mission_phase_status_sql(node):
            offenders.append(f"{relative}:{node.lineno}")
    return offenders


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def _is_update_phase_status_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return isinstance(node.func, ast.Attribute) and node.func.attr == "update_phase_status"


def _is_raw_mission_phase_status_sql(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    normalized = " ".join(node.value.lower().split())
    return "mission_phases" in normalized and "status" in normalized and (
        "update mission_phases" in normalized
        or "insert into mission_phases" in normalized
        or "delete from mission_phases" in normalized
    )


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str | None:
    target_line = getattr(target, "lineno", -1)
    candidates: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= target_line <= (node.end_lineno or node.lineno)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda node: node.lineno).name


def test_no_direct_phase_status_writes_outside_hunger_engine_tick() -> None:
    offenders = [
        offender
        for path in sorted(SRC_ROOT.rglob("*.py"))
        for offender in _phase_status_write_offenders(path)
    ]

    assert offenders == []


def test_synthetic_direct_phase_status_write_is_caught(tmp_path: Path) -> None:
    offender = tmp_path / "bad_writer.py"
    offender.write_text(
        "from __future__ import annotations\n"
        "def bad(repo):\n"
        "    repo.update_phase_status('phase-1', 'done')\n",
        encoding="utf-8",
    )

    assert _phase_status_write_offenders(offender) == [f"{offender}:3"]
