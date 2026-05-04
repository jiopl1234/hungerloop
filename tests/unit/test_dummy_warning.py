"""DummyModelClient warning policy tests (PRD §6.4).

The contract:

* ``ModelConfigLoader.load()`` on YAML with ``provider: dummy`` writes
  the standard warning to stderr.
* ``HUNGERLOOP_QUIET_DUMMY=1`` (or ``true`` / ``yes``) suppresses it.
* Test injection — constructing ``ModelConfig(provider=DUMMY)`` directly
  or passing a pre-built ``DummyModelClient`` via ``CliContext`` —
  bypasses the loader and stays silent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.hunger import HungerLedger, HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.model_config import ModelConfigLoader


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "model.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loader-driven warning
# ---------------------------------------------------------------------------


def test_yaml_dummy_emits_stderr_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HUNGERLOOP_QUIET_DUMMY", raising=False)
    yaml_path = _write_yaml(tmp_path, "provider: dummy\nmodel_name: dummy\n")
    ModelConfigLoader().load(yaml_path)
    err = capsys.readouterr().err
    assert "DummyModelClient" in err
    assert "scripted" in err


def test_yaml_dummy_silenced_by_env_var(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUNGERLOOP_QUIET_DUMMY", "1")
    yaml_path = _write_yaml(tmp_path, "provider: dummy\nmodel_name: dummy\n")
    ModelConfigLoader().load(yaml_path)
    assert "DummyModelClient" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["true", "yes", "TRUE", "Yes", " 1 "])
def test_yaml_dummy_silenced_by_truthy_strings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """A few common truthy spellings all silence the warning."""
    monkeypatch.setenv("HUNGERLOOP_QUIET_DUMMY", value)
    yaml_path = _write_yaml(tmp_path, "provider: dummy\nmodel_name: dummy\n")
    ModelConfigLoader().load(yaml_path)
    assert "DummyModelClient" not in capsys.readouterr().err


def test_falsy_quiet_dummy_does_not_silence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``HUNGERLOOP_QUIET_DUMMY=0`` (or empty) leaves the warning intact."""
    monkeypatch.setenv("HUNGERLOOP_QUIET_DUMMY", "0")
    yaml_path = _write_yaml(tmp_path, "provider: dummy\nmodel_name: dummy\n")
    ModelConfigLoader().load(yaml_path)
    assert "DummyModelClient" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Test-injection silence
# ---------------------------------------------------------------------------


def test_test_injection_path_is_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runner.invoke(cli, [...], obj=ctx)`` with a pre-built
    ``DummyModelClient`` never touches the loader — so the warning never
    fires (PRD §6.4 test-injection clause).
    """
    monkeypatch.delenv("HUNGERLOOP_QUIET_DUMMY", raising=False)
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "demo-1",
        HungerPolicy(
            max_total_cost_usd=1.0,
            max_total_tokens=10_000,
            initial_hunger=10.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.save_hunger_ledger(
        "demo-1", HungerLedger(task_id="demo-1", items=[])
    )
    ctx = CliContext(
        repo=repo,
        workspace_root=tmp_path,
        model_client=DummyModelClient.with_actions([]),
    )
    runner = CliRunner()
    # ``status`` is a read-only command that doesn't itself instantiate a
    # client; the assertion is on capsys for any incidental loader path.
    runner.invoke(cli, ["status", "demo-1"], obj=ctx)
    assert "DummyModelClient" not in capsys.readouterr().err
