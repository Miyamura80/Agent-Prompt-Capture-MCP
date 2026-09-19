"""Shared fixtures. Every test runs against a throwaway ``APC_HOME``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_prompt_capture import config as config_mod  # noqa: E402
from agent_prompt_capture.config import Config  # noqa: E402
from agent_prompt_capture.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def apc_home(tmp_path, monkeypatch):
    """Point ``$APC_HOME`` (and ``$HOME``) at a tmp dir for the whole test."""
    home = tmp_path / "apc-home"
    home.mkdir(parents=True, exist_ok=True)
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APC_HOME", str(home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("APC_DEBUG", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config_mod, "_LOGGING_CONFIGURED", False, raising=False)
    return home


@pytest.fixture
def config(apc_home) -> Config:
    return Config(home=apc_home)


@pytest.fixture
def store(apc_home):
    db = Store(apc_home / "prompts.db")
    yield db
    db.close()


@pytest.fixture
def write_config(apc_home):
    def _write(text: str) -> Config:
        (apc_home / "config.toml").write_text(text, encoding="utf-8")
        return config_mod.load_config(apc_home)

    return _write
