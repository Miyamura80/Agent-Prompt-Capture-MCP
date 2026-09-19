"""Config loading, token management, account resolution and logging."""

from __future__ import annotations

import hashlib
import logging
import stat

from agent_prompt_capture import config as config_mod
from agent_prompt_capture.config import Config, apc_home, ensure_home, load_config, setup_logging

SAMPLE = """
[capture]
allowed_accounts = ["me@work.com", "Other@Example.COM"]
disabled_sources = ["chatgpt_web"]

[accounts]
"me@work.com" = "work"

[pii]
extra_terms = ["acme"]
extra_patterns = ["PROJ-\\\\d+"]
enable_ner = false

[server]
host = "127.0.0.1"
port = 47999

[time]
idle_gap_minutes = 45
tail_minutes = 2
"""


def test_apc_home_honours_env(apc_home):
    assert apc_home == apc_home  # sanity
    assert config_mod.apc_home() == apc_home


def test_apc_home_default(monkeypatch):
    monkeypatch.delenv("APC_HOME", raising=False)
    assert apc_home().name == ".agent-prompt-capture"


def test_ensure_home_is_0700(tmp_path):
    target = tmp_path / "nested" / "home"
    ensure_home(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_defaults_without_config_file(apc_home):
    cfg = load_config(apc_home)
    assert cfg.allowed_accounts == []
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 47821
    assert cfg.idle_gap_minutes == 30.0
    assert cfg.tail_minutes == 5.0


def test_full_config(write_config):
    cfg = write_config(SAMPLE)
    assert cfg.allowed_accounts == ["me@work.com", "Other@Example.COM"]
    assert cfg.disabled_sources == ["chatgpt_web"]
    assert cfg.account_aliases == {"me@work.com": "work"}
    assert cfg.extra_terms == ["acme"]
    assert cfg.extra_patterns == [r"PROJ-\d+"]
    assert cfg.enable_ner is False
    assert cfg.port == 47999
    assert cfg.idle_gap_minutes == 45.0
    assert cfg.tail_minutes == 2.0


def test_broken_config_falls_back_to_defaults(apc_home):
    (apc_home / "config.toml").write_text("this is [not toml", encoding="utf-8")
    cfg = load_config(apc_home)
    assert cfg.allowed_accounts == []
    assert cfg.port == 47821


def test_is_account_allowed_is_case_insensitive(write_config):
    cfg = write_config(SAMPLE)
    assert cfg.is_account_allowed("me@work.com")
    assert cfg.is_account_allowed("ME@WORK.COM")
    assert cfg.is_account_allowed("  me@work.com  ")
    assert cfg.is_account_allowed("other@example.com")
    assert not cfg.is_account_allowed("someone@else.com")
    assert not cfg.is_account_allowed(None)
    assert not cfg.is_account_allowed("")


def test_resolve_account_alias(write_config):
    cfg = write_config(SAMPLE)
    assert cfg.resolve_account("me@work.com") == "work"
    assert cfg.resolve_account("ME@WORK.COM") == "work"


def test_resolve_account_hash(config):
    resolved = config.resolve_account("Someone@Example.com")
    expected = "sha256:" + hashlib.sha256(b"someone@example.com").hexdigest()[:12]
    assert resolved == expected
    assert len(resolved) == len("sha256:") + 12
    assert config.resolve_account("someone@example.com") == resolved
    assert config.resolve_account(None) is None
    assert config.resolve_account("  ") is None


def test_disabled_sources(write_config):
    from agent_prompt_capture.models import Source

    cfg = write_config(SAMPLE)
    assert cfg.is_source_disabled(Source.CHATGPT_WEB)
    assert not cfg.is_source_disabled(Source.CLAUDE_WEB)
    assert cfg.is_source_disabled("chatgpt_web")


def test_token_round_trip(config):
    token = config.get_token()
    assert len(token) >= 32
    assert config.get_token() == token
    assert stat.S_IMODE(config.token_path.stat().st_mode) == 0o600


def test_rotate_token(config):
    first = config.get_token()
    second = config.rotate_token()
    assert first != second
    assert config.get_token() == second


def test_get_token_regenerates_when_blank(config):
    config.get_token()
    config.token_path.write_text("\n", encoding="utf-8")
    assert config.get_token().strip()


def test_setup_logging_never_touches_stdout(apc_home, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "_LOGGING_CONFIGURED", False)
    logger = setup_logging(apc_home, force=True)
    logger.info("hello from the test")
    for handler in logger.handlers:
        stream = getattr(handler, "stream", None)
        assert stream is None or stream.name not in ("<stdout>",)
    assert capsys.readouterr().out == ""
    assert (apc_home / "apc.log").exists()
    assert "hello from the test" in (apc_home / "apc.log").read_text()


def test_setup_logging_adds_stderr_when_debugging(apc_home, monkeypatch):
    monkeypatch.setenv("APC_DEBUG", "1")
    monkeypatch.setattr(config_mod, "_LOGGING_CONFIGURED", False)
    logger = setup_logging(apc_home, force=True)
    assert logger.level == logging.DEBUG
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)


def test_config_paths(config, apc_home):
    assert config.db_path == apc_home / "prompts.db"
    assert config.token_path == apc_home / "token"
    assert config.log_path == apc_home / "apc.log"
    assert config.config_path == apc_home / "config.toml"


def test_config_load_classmethod(apc_home):
    assert isinstance(Config.load(apc_home), Config)
