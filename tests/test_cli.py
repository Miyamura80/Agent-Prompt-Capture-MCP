"""CLI: capture hard rules, output formats, export, purge, doctor."""

from __future__ import annotations

import csv
import io
import json

import pytest
from helpers import make_record

from agent_prompt_capture.cli import build_parser, main
from agent_prompt_capture.store import Store

CLAUDE = {
    "session_id": "abc123",
    "cwd": "/Users/alice/dev/proj",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "email alice@example.com about the parser",
}

CODEX_NOTIFY = {
    "type": "agent-turn-complete",
    "thread-id": "th-1",
    "turn-id": "12345",
    "cwd": "/Users/alice/dev/proj",
    "client": "codex-tui",
    "input-messages": ["ship the release"],
    "last-assistant-message": "done",
}


@pytest.fixture
def stdin(monkeypatch):
    def _set(text: str) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

    return _set


def db(apc_home) -> Store:
    return Store(apc_home / "prompts.db")


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_capture_stores_a_scrubbed_prompt(apc_home, stdin, capsys):
    stdin(json.dumps(CLAUDE))
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr().out == ""
    store = db(apc_home)
    records = store.list()
    store.close()
    assert len(records) == 1
    assert "alice@example.com" not in records[0].prompt
    assert records[0].cwd == "/Users/[USER]/dev/proj"


def test_capture_never_prints(apc_home, stdin, capsys):
    stdin(json.dumps(CLAUDE))
    main(["capture", "claude-code"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_capture_with_empty_stdin(apc_home, stdin, capsys):
    stdin("")
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_capture_with_whitespace_stdin(apc_home, stdin):
    stdin("   \n  ")
    assert main(["capture", "claude-code"]) == 0


def test_capture_with_invalid_json(apc_home, stdin, capsys):
    stdin("{not json")
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_capture_with_a_bad_event_never_raises(apc_home, stdin, capsys):
    stdin(json.dumps({**CLAUDE, "hook_event_name": "PreToolUse"}))
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_capture_logs_failures_to_the_log_file(apc_home, stdin):
    stdin(json.dumps({**CLAUDE, "hook_event_name": "PreToolUse"}))
    main(["capture", "claude-code"])
    assert "capture failed" in (apc_home / "apc.log").read_text()


def test_capture_codex_from_stdin(apc_home, stdin):
    stdin(
        json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "from stdin",
                "session_id": "s-stdin",
                "cwd": "/Users/alice/p",
                "model": "gpt-5.1-codex",
                "permission_mode": "default",
                "turn_id": "1",
                "transcript_path": None,
            }
        )
    )
    assert main(["capture", "codex"]) == 0
    store = db(apc_home)
    records = store.list()
    store.close()
    assert [r.prompt for r in records] == ["from stdin"]


def test_capture_codex_from_the_trailing_argv_argument(apc_home, monkeypatch):
    """Codex's legacy notify delivers the JSON as argv[last] with stdin closed."""

    class _ClosedStdin:
        def read(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr("sys.stdin", _ClosedStdin())
    assert main(["capture", "codex", json.dumps(CODEX_NOTIFY)]) == 0
    store = db(apc_home)
    records = store.list()
    store.close()
    assert [r.prompt for r in records] == ["ship the release"]
    assert records[0].session_id == "th-1"
    assert records[0].turn_end_ts is not None


def test_capture_prefers_argv_when_stdin_is_empty(apc_home, stdin):
    stdin("")
    assert main(["capture", "codex", json.dumps(CODEX_NOTIFY)]) == 0
    store = db(apc_home)
    count = store.count()
    store.close()
    assert count == 1


def test_capture_ignores_a_non_json_argv_argument(apc_home, stdin):
    stdin(json.dumps({**CLAUDE, "prompt": "from stdin wins"}))
    assert main(["capture", "claude-code", "not-json"]) == 0
    store = db(apc_home)
    records = store.list()
    store.close()
    assert [r.prompt for r in records] == ["from stdin wins"]


def test_capture_opencode_turn_end(apc_home, stdin):
    stdin(json.dumps({"prompt": "do a thing", "session_id": "ses_1"}))
    main(["capture", "opencode"])
    stdin(json.dumps({"event": "turn_end", "session_id": "ses_1", "ts": "2026-09-19T21:00:00Z"}))
    main(["capture", "opencode"])
    store = db(apc_home)
    records = store.list()
    store.close()
    assert records[0].turn_end_ts == "2026-09-19T21:00:00.000Z"


# ---------------------------------------------------------------------------
# read commands
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(apc_home):
    store = db(apc_home)
    store.insert(
        make_record(
            "refactor the payment module",
            ts="2026-09-19T09:00:00.000Z",
            turn_end_ts="2026-09-19T09:04:00.000Z",
            project="alpha",
        )
    )
    store.insert(
        make_record("write the parser tests", ts="2026-09-19T09:20:00.000Z", project="beta")
    )
    store.close()
    return apc_home


def test_list_table(seeded, capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "source" in out
    assert "refactor the payment module" in out


def test_list_json(seeded, capsys):
    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert payload["prompts"][0]["source"] == "claude_code"


def test_list_empty(apc_home, capsys):
    main(["list"])
    assert "no prompts" in capsys.readouterr().out


def test_list_filters(seeded, capsys):
    main(["list", "--project", "beta", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1


def test_search_table_and_json(seeded, capsys):
    main(["search", "payment"])
    assert "refactor" in capsys.readouterr().out
    main(["search", "payment", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert "rank" in payload["results"][0]


def test_search_no_matches(seeded, capsys):
    main(["search", "zzzznothing"])
    assert "no matches" in capsys.readouterr().out


def test_stats(seeded, capsys):
    main(["stats", "--group-by", "project"])
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    main(["stats", "--group-by", "project", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {g["key"] for g in payload["groups"]} == {"alpha", "beta"}


def test_time(seeded, capsys):
    main(["time", "--since", "2026-09-19T00:00:00Z", "--group-by", "project"])
    out = capsys.readouterr().out
    assert "context switches" in out
    assert "alpha" in out


def test_time_json(seeded, capsys):
    main(["time", "--since", "2026-09-19T00:00:00Z", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["group_by"] == "project"
    assert payload["total_active_minutes"] > 0


def test_time_empty(apc_home, capsys):
    main(["time"])
    assert "no activity" in capsys.readouterr().out


def test_digest(seeded, capsys):
    main(["digest", "2026-09-19"])
    out = capsys.readouterr().out
    assert "digest for 2026-09-19" in out
    assert "context switches" in out
    assert "top terms" in out


def test_digest_json(seeded, capsys):
    main(["digest", "2026-09-19", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["prompt_count"] == 2


def test_digest_bad_date(seeded, capsys):
    assert main(["digest", "nope"]) == 2
    assert "apc:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# export / purge / token / doctor
# ---------------------------------------------------------------------------


def test_export_jsonl(seeded, capsys):
    main(["export"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["ts"] == "2026-09-19T09:00:00.000Z"


def test_export_csv(seeded, capsys):
    main(["export", "--format", "csv"])
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 2
    assert rows[0]["source"] == "claude_code"
    assert "turn_end_ts" in rows[0]


def test_export_to_a_file(seeded, tmp_path):
    target = tmp_path / "out.jsonl"
    main(["export", "--output", str(target)])
    assert len(target.read_text().strip().splitlines()) == 2


def test_export_since(seeded, capsys):
    main(["export", "--since", "2026-09-19T09:10:00Z"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1


def test_purge_requires_a_filter(seeded, capsys):
    assert main(["purge", "--yes"]) == 2
    assert "refusing to purge everything" in capsys.readouterr().err


def test_purge_with_yes(seeded, capsys):
    assert main(["purge", "--before", "2026-09-19T23:00:00Z", "--yes"]) == 0
    assert "deleted 2" in capsys.readouterr().out


def test_purge_by_source(seeded, capsys):
    main(["purge", "--source", "claude_code", "--yes"])
    assert "deleted 2" in capsys.readouterr().out


def test_purge_without_yes_is_refused_non_interactively(seeded, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["purge", "--source", "claude_code"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_purge_interactive_confirmation(seeded, capsys, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", _Tty(""))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert main(["purge", "--source", "claude_code"]) == 0
    assert "deleted 2" in capsys.readouterr().out


def test_purge_interactive_abort(seeded, capsys, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", _Tty(""))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["purge", "--source", "claude_code"]) == 1
    assert "aborted" in capsys.readouterr().out


def test_token(apc_home, capsys):
    main(["token"])
    first = capsys.readouterr().out.strip()
    assert len(first) >= 32
    main(["token"])
    assert capsys.readouterr().out.strip() == first
    main(["token", "--rotate"])
    assert capsys.readouterr().out.strip() != first


def test_install_and_uninstall_through_the_cli(apc_home, capsys):
    assert main(["install", "claude-code", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert main(["install", "codex"]) == 0
    assert "hooks.json" in capsys.readouterr().out
    assert main(["install", "codex", "--legacy"]) == 0
    assert "notify" in capsys.readouterr().out
    assert main(["uninstall", "codex"]) == 0
    assert "removed" in capsys.readouterr().out


def test_doctor(apc_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "APC_HOME" in out
    assert "database" in out
    assert "token" in out
    assert "listener" in out
    assert "claude-code hooks" in out
    assert "codex hooks" in out
    assert "opencode plugin" in out
    assert "claude.ai/code" in out  # the web-hooks caveat


def test_doctor_warns_about_double_codex_capture(apc_home, capsys):
    main(["install", "codex"])
    main(["install", "codex", "--legacy"])
    capsys.readouterr()
    main(["doctor"])
    out = capsys.readouterr().out
    assert "[warn]" in out
    assert "captured twice" in out


def test_doctor_warns_about_an_empty_allowlist(apc_home, capsys):
    main(["doctor"])
    assert "allowlist is empty" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parser_has_every_subcommand():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions
    expected = {
        "capture",
        "serve",
        "mcp",
        "install",
        "uninstall",
        "token",
        "list",
        "search",
        "stats",
        "time",
        "digest",
        "export",
        "purge",
        "doctor",
    }
    assert set(actions[0].choices) == expected


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "apc" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# regressions: `apc capture` hard rules
# ---------------------------------------------------------------------------

BAD_PAYLOADS = [
    "",
    "   \n  ",
    "not json at all",
    "[1, 2, 3]",
    '"just a string"',
    "null",
    "{}",
    '{"hook_event_name": "PreToolUse"}',
    '{"hook_event_name": "UserPromptSubmit"}',
    '{"hook_event_name": "UserPromptSubmit", "prompt": null}',
    '{"hook_event_name": "UserPromptSubmit", "prompt": 42}',
    '{"hook_event_name": "UserPromptSubmit", "prompt": "   "}',
    '{"prompt": "x", "cwd": 1234}',
    '{"hook_event_name": "UserPromptSubmit", "prompt": "hi", "session_id": {"a": 1}}',
]


@pytest.mark.parametrize("payload", BAD_PAYLOADS, ids=range(len(BAD_PAYLOADS)))
@pytest.mark.parametrize("target", ["claude-code", "codex", "opencode"])
def test_capture_is_silent_and_exits_zero_on_anything(apc_home, stdin, capsys, target, payload):
    """Exit 2 erases the user's prompt; stdout is injected into their context."""
    stdin(payload)
    assert main(["capture", target]) == 0
    captured = capsys.readouterr()
    assert captured.out == "", f"{target}/{payload!r} wrote to stdout"
    assert captured.err == "", f"{target}/{payload!r} wrote to stderr"


def test_capture_survives_a_closed_stdin(apc_home, monkeypatch, capsys):
    """Codex's legacy notify runs the program with stdin closed."""

    class Closed(io.StringIO):
        def read(self, *_args):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr("sys.stdin", Closed())
    assert main(["capture", "codex"]) == 0
    assert capsys.readouterr().out == ""


def test_capture_survives_an_unwritable_apc_home(monkeypatch, stdin, capsys):
    """A read-only home breaks logging AND the database; the hook still exits 0.

    ``ensure_home`` is patched rather than relying on file modes, because the test
    may run as a user (root, CI images) for whom chmod means nothing.
    """
    import agent_prompt_capture.config as config_mod

    def denied(*_args, **_kwargs):
        raise PermissionError("read-only home")

    monkeypatch.setattr(config_mod, "ensure_home", denied)
    monkeypatch.setattr(config_mod, "_LOGGING_CONFIGURED", False, raising=False)
    stdin(json.dumps(CLAUDE))
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr() == ("", "")


def test_setup_logging_never_raises_on_an_unwritable_home(monkeypatch):
    import logging

    import agent_prompt_capture.config as config_mod

    def denied(*_args, **_kwargs):
        raise PermissionError("read-only home")

    monkeypatch.setattr(config_mod, "ensure_home", denied)
    logger = config_mod.setup_logging(force=True)
    assert isinstance(logger, logging.Logger)
    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)
    logger.info("this must not raise")


def test_capture_survives_a_store_that_explodes(apc_home, stdin, capsys, monkeypatch):
    import agent_prompt_capture.store as store_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store_mod.Store, "__init__", boom)
    stdin(json.dumps(CLAUDE))
    assert main(["capture", "claude-code"]) == 0
    assert capsys.readouterr() == ("", "")


def test_capture_from_argv_never_touches_stdin(apc_home, monkeypatch, capsys):
    """The notify program passes JSON as the final argv argument, stdin closed."""

    def explode(*_args, **_kwargs):
        raise AssertionError("stdin must not be read when argv carries the payload")

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.read", explode)
    assert main(["capture", "codex", json.dumps(CODEX_NOTIFY)]) == 0
    assert capsys.readouterr() == ("", "")
    store = db(apc_home)
    try:
        records = store.list()
        assert len(records) == 1
        assert records[0].prompt == "ship the release"
        assert records[0].metadata["turn_id"] == "12345"
    finally:
        store.close()


@pytest.mark.parametrize(
    "argument", ["", "   ", "not json", "[1,2]", "{oops", '"string"', "{}extra"]
)
def test_capture_falls_back_to_stdin_for_a_non_object_argv(apc_home, stdin, argument, capsys):
    stdin(json.dumps(CLAUDE))
    assert main(["capture", "claude-code", argument]) == 0
    assert capsys.readouterr() == ("", "")
    store = db(apc_home)
    try:
        assert store.count() == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# regressions: export / purge never leak
# ---------------------------------------------------------------------------


def test_export_only_emits_scrubbed_fields(apc_home, stdin, capsys):
    stdin(json.dumps(CLAUDE))
    main(["capture", "claude-code"])
    capsys.readouterr()

    main(["export", "--format", "jsonl"])
    out = capsys.readouterr().out
    assert "alice@example.com" not in out
    assert "/Users/alice" not in out
    assert "[EMAIL_1]" in out
    assert "/Users/[USER]/dev/proj" in out

    main(["export", "--format", "csv"])
    csv_out = capsys.readouterr().out
    assert "alice@example.com" not in csv_out
    assert "/Users/alice" not in csv_out


def test_export_carries_no_local_paths_of_its_own(apc_home, stdin, capsys):
    """Nothing from the runtime config (APC_HOME, db path) belongs in an export."""
    stdin(json.dumps(CLAUDE))
    main(["capture", "claude-code"])
    capsys.readouterr()
    main(["export", "--format", "jsonl"])
    out = capsys.readouterr().out
    assert str(apc_home) not in out
    assert "prompts.db" not in out


def test_purge_reports_only_a_count(apc_home, stdin, capsys):
    stdin(json.dumps(CLAUDE))
    main(["capture", "claude-code"])
    capsys.readouterr()
    assert main(["purge", "--source", "claude_code", "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "deleted 1 prompt(s)"
    assert "alice" not in out
