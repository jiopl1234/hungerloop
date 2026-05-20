from __future__ import annotations

import ast
import re
from pathlib import Path

UPDATER_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "hungerloop"
    / "services"
    / "mission_state_updater.py"
)


def _source() -> str:
    return UPDATER_PATH.read_text(encoding="utf-8")


def test_forbid_yaml_load() -> None:
    assert not re.search(r"yaml\.(safe_)?load|yaml\.unsafe_load|yaml\.full_load", _source())


def test_forbid_repo_writes() -> None:
    tree = ast.parse(_source())
    forbidden_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        value = func.value
        if isinstance(value, ast.Attribute) and value.attr == "repo":
            name = func.attr
        elif isinstance(value, ast.Name) and value.id == "repo":
            name = func.attr
        else:
            continue
        if name.startswith(("save_", "update_", "delete_")):
            forbidden_calls.append(name)
    assert forbidden_calls == []


def test_forbid_candidate_artifact_reads() -> None:
    source = _source()
    assert "candidate" not in source.lower()
    assert ".read_text(" not in source
    assert ".read_bytes(" not in source
