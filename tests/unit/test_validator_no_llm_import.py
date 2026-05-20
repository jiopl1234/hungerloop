from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "src" / "hungerloop" / "services" / "validators"

_FORBIDDEN_MODULES = {
    "hungerloop.services.model_client",
    "hungerloop.services.openai_model_client",
}
_FORBIDDEN_NAMES = {"ModelClient", "OpenAIModelClient", "DummyModelClient"}


def _is_forbidden_module(module_name: str) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN_MODULES
    )


def _import_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    offenders.append(f"{_display_path(path)}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names = {alias.name for alias in node.names}
            if _is_forbidden_module(module) or (
                module == "hungerloop.services"
                and imported_names.intersection({"model_client", "openai_model_client"})
            ):
                offenders.append(f"{_display_path(path)}:{node.lineno}")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = _base_name(base)
                if base_name in _FORBIDDEN_NAMES:
                    offenders.append(f"{_display_path(path)}:{node.lineno}")
    return offenders


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_validators_do_not_import_llm_clients() -> None:
    offenders = [
        offender
        for path in sorted(VALIDATORS_DIR.rglob("*.py"))
        for offender in _import_offenders(path)
    ]

    assert offenders == []


def test_synthetic_model_client_import_is_caught(tmp_path: Path) -> None:
    offender = tmp_path / "bad_validator.py"
    offender.write_text(
        "from __future__ import annotations\n"
        "from hungerloop.services.model_client import ModelClient\n"
        "class BadValidator(ModelClient):\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert _import_offenders(offender) == [
        f"{offender}:2",
        f"{offender}:3",
    ]
