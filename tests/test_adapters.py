"""Each adapter against each documented payload shape."""

from __future__ import annotations

import pytest

from agent_prompt_capture.adapters import AdapterError, RawPrompt, RawTurnEnd
from agent_prompt_capture.adapters import browser as browser_adapter
from agent_prompt_capture.adapters import claude_code as cc
from agent_prompt_capture.adapters import codex as codex_adapter
from agent_prompt_capture.adapters import opencode as oc
from agent_prompt_capture.models import Source

# ---------------------------------------------------------------------------
# claude code
# ---------------------------------------------------------------------------

CLAUDE_PAYLOAD = {
    "session_id": "abc123",
    "transcript_path": "/Users/alice/.claude/projects/x/abc123.jsonl",
    "cwd": "/Users/alice/dev/proj",
    "permission_mode": "default",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "the user's text",
}


def test_claude_code_prompt():
    result = cc.parse(CLAUDE_PAYLOAD)
    assert isinstance(result, RawPrompt)
    assert result.prompt == "the user's text"
    assert result.session_id == "abc123"
    assert result.cwd == "/Users/alice/dev/proj"
    assert result.project == "proj"
    assert result.metadata["permission_mode"] == "default"


def test_claude_code_metadata_extras():
    result = cc.parse(
        {
            **CLAUDE_PAYLOAD,
            "prompt_id": "p-77",
            "agent_type": "general-purpose",
            "model": "opus",
        }
    )
    assert result.metadata["prompt_id"] == "p-77"
    assert result.metadata["agent_type"] == "general-purpose"
    assert result.metadata["model"] == "opus"


def test_claude_code_transcript_path_is_scrubbed():
    result = cc.parse(CLAUDE_PAYLOAD)
    assert result.metadata["transcript_path"] == "/Users/[USER]/.claude/projects/x/abc123.jsonl"
    assert "alice" not in result.metadata["transcript_path"]


def test_claude_code_stop_event():
    result = cc.parse(
        {"session_id": "abc123", "hook_event_name": "Stop", "last_assistant_message": "done"}
    )
    assert isinstance(result, RawTurnEnd)
    assert result.session_id == "abc123"


def test_claude_code_stop_with_stop_hook_active_is_ignored():
    payload = {"session_id": "abc123", "hook_event_name": "Stop", "stop_hook_active": True}
    assert cc.parse(payload) is None
    assert isinstance(cc.parse({**payload, "stop_hook_active": False}), RawTurnEnd)


def test_claude_code_empty_prompt_is_skipped():
    assert cc.parse({**CLAUDE_PAYLOAD, "prompt": "   "}) is None
    assert cc.parse({**CLAUDE_PAYLOAD, "prompt": ""}) is None
    assert cc.parse({**CLAUDE_PAYLOAD, "prompt": None}) is None


@pytest.mark.parametrize("event", ["PreToolUse", "SessionStart", "", None, "userpromptsubmit"])
def test_claude_code_other_events_raise(event):
    with pytest.raises(AdapterError):
        cc.parse({**CLAUDE_PAYLOAD, "hook_event_name": event})


def test_claude_code_rejects_non_objects():
    with pytest.raises(AdapterError):
        cc.parse(["not", "a", "dict"])


def test_claude_code_ignores_unknown_keys():
    result = cc.parse({**CLAUDE_PAYLOAD, "brand_new_field": {"a": 1}})
    assert isinstance(result, RawPrompt)


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

# (a) native hooks UserPromptSubmit, verbatim shape from docs/research/hook-specs.md 2.3
CODEX_HOOK = {
    "session_id": "b5f6c1c2-1111-2222-3333-444455556666",
    "turn_id": "12345",
    "transcript_path": "/Users/alice/.codex/sessions/2026/09/19/rollout-x.jsonl",
    "cwd": "/Users/alice/dev/proj",
    "model": "gpt-5.1-codex",
    "permission_mode": "default",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "Rename `foo` to `bar` and update the callsites.",
}

# (c) legacy notify, verbatim from the crate's own test in hook-specs.md 2.5
CODEX_NOTIFY = {
    "type": "agent-turn-complete",
    "thread-id": "b5f6c1c2-1111-2222-3333-444455556666",
    "turn-id": "12345",
    "cwd": "/Users/example/project",
    "client": "codex-tui",
    "input-messages": ["Rename `foo` to `bar` and update the callsites."],
    "last-assistant-message": "Rename complete and verified `cargo build` succeeds.",
}


def test_codex_hooks_user_prompt_submit():
    result = codex_adapter.parse(CODEX_HOOK)
    assert isinstance(result, RawPrompt)
    assert result.prompt == CODEX_HOOK["prompt"]
    assert result.session_id == CODEX_HOOK["session_id"]
    assert result.cwd == "/Users/alice/dev/proj"
    assert result.project == "proj"
    assert result.metadata["turn_id"] == "12345"
    assert result.metadata["model"] == "gpt-5.1-codex"
    assert result.metadata["permission_mode"] == "default"
    assert result.turn_end_ts is None


def test_codex_hooks_transcript_path_is_scrubbed():
    result = codex_adapter.parse(CODEX_HOOK)
    assert "alice" not in result.metadata["transcript_path"]
    assert result.metadata["transcript_path"].startswith("/Users/[USER]/")


def test_codex_hooks_nullable_transcript_path():
    result = codex_adapter.parse({**CODEX_HOOK, "transcript_path": None})
    assert "transcript_path" not in result.metadata


def test_codex_hooks_stop_event():
    result = codex_adapter.parse(
        {"hook_event_name": "Stop", "session_id": "sess-1", "turn_id": "9"}
    )
    assert isinstance(result, RawTurnEnd)
    assert result.session_id == "sess-1"
    assert result.metadata["turn_id"] == "9"


@pytest.mark.parametrize("event", ["PreToolUse", "SessionStart", "SubagentStop", "PostCompact"])
def test_codex_other_hook_events_are_ignored(event):
    assert codex_adapter.parse({**CODEX_HOOK, "hook_event_name": event}) is None


def test_codex_hooks_empty_prompt():
    assert codex_adapter.parse({**CODEX_HOOK, "prompt": "  "}) is None


def test_codex_bare_prompt_payload():
    result = codex_adapter.parse(
        {"prompt": "fix the bug", "session_id": "sess-1", "cwd": "/home/bob/app"}
    )
    assert isinstance(result, RawPrompt)
    assert result.prompt == "fix the bug"
    assert result.project == "app"


@pytest.mark.parametrize("key", ["session_id", "thread_id", "thread-id", "threadId"])
def test_codex_session_key_variants(key):
    result = codex_adapter.parse({"prompt": "hi", key: "t-9"})
    assert result.session_id == "t-9"


def test_codex_notify_payload():
    result = codex_adapter.parse(CODEX_NOTIFY)
    assert isinstance(result, RawPrompt)
    assert result.prompt == CODEX_NOTIFY["input-messages"][0]
    assert result.session_id == CODEX_NOTIFY["thread-id"]
    assert result.cwd == "/Users/example/project"
    assert result.metadata["turn_id"] == "12345"
    assert result.metadata["client"] == "codex-tui"
    assert result.metadata["input_message_count"] == 1
    assert result.metadata["has_assistant_reply"] is True
    assert result.turn_end_ts
    assert result.metadata["turn_complete_ts"] == result.turn_end_ts


def test_codex_notify_takes_the_last_non_blank_message():
    result = codex_adapter.parse(
        {"type": "agent-turn-complete", "input-messages": ["a", "b", "c", "   "]}
    )
    assert result.prompt == "c"
    assert result.metadata["input_message_count"] == 4


def test_codex_notify_without_messages():
    assert codex_adapter.parse({"type": "agent-turn-complete", "input-messages": []}) is None
    assert codex_adapter.parse({"type": "agent-turn-complete"}) is None


@pytest.mark.parametrize("kind", ["agent-turn-failed", "session-start", "something-else"])
def test_codex_other_types_are_ignored(kind):
    assert codex_adapter.parse({"type": kind, "thread-id": "x"}) is None


def test_codex_tolerates_unknown_keys():
    result = codex_adapter.parse({**CODEX_HOOK, "totally": "new", "nested": {"a": [1]}})
    assert isinstance(result, RawPrompt)


def test_codex_empty_payload():
    assert codex_adapter.parse({}) is None


def test_codex_rejects_non_objects():
    with pytest.raises(AdapterError):
        codex_adapter.parse("nope")


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------


def test_opencode_prompt():
    result = oc.parse(
        {
            "session_id": "ses_1",
            "cwd": "/home/bob/app",
            "project": "app",
            "model": "claude-sonnet",
            "prompt": "concatenated text parts",
            "ts": "2026-09-19T20:11:03.123Z",
        }
    )
    assert isinstance(result, RawPrompt)
    assert result.project == "app"
    assert result.metadata["model"] == "claude-sonnet"
    assert result.ts == "2026-09-19T20:11:03.123Z"


@pytest.mark.parametrize("event", ["turn_end", "session.idle"])
def test_opencode_turn_end(event):
    result = oc.parse({"event": event, "session_id": "ses_1", "ts": "2026-09-19T20:12:00.000Z"})
    assert isinstance(result, RawTurnEnd)
    assert result.session_id == "ses_1"
    assert result.ts == "2026-09-19T20:12:00.000Z"


def test_opencode_empty_prompt():
    assert oc.parse({"session_id": "s", "prompt": ""}) is None
    assert oc.parse({}) is None


def test_opencode_project_falls_back_to_cwd():
    result = oc.parse({"prompt": "x", "cwd": "/home/bob/thing"})
    assert result.project == "thing"


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

BROWSER_PAYLOAD = {
    "source": "claude_web",
    "prompt": "hello there",
    "account": "me@work.com",
    "conversation_id": "conv-1",
    "url": "https://claude.ai/chat/uuid?foo=bar#frag",
    "title": "conversation title",
    "ts": "2026-09-19T20:11:03.123Z",
    "client_version": "0.1.0",
}


def test_browser_payload():
    result = browser_adapter.parse(BROWSER_PAYLOAD)
    assert isinstance(result, RawPrompt)
    assert result.session_id == "conv-1"
    assert result.project == "conversation title"
    assert result.account == "me@work.com"
    assert result.metadata["url"] == "https://claude.ai/chat/uuid"
    assert result.metadata["client_version"] == "0.1.0"


@pytest.mark.parametrize("source", ["claude_web", "claude_code_web", "chatgpt_web", "codex_cloud"])
def test_browser_accepts_the_four_sources(source):
    result = browser_adapter.parse({**BROWSER_PAYLOAD, "source": source})
    assert isinstance(result, RawPrompt)


@pytest.mark.parametrize("source", ["claude_code", "opencode", "banana", None, ""])
def test_browser_rejects_other_sources(source):
    with pytest.raises(AdapterError):
        browser_adapter.parse({**BROWSER_PAYLOAD, "source": source})


def test_browser_source_mismatch():
    with pytest.raises(AdapterError):
        browser_adapter.parse(BROWSER_PAYLOAD, source=Source.CHATGPT_WEB)


def test_browser_empty_prompt():
    assert browser_adapter.parse({**BROWSER_PAYLOAD, "prompt": " "}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://claude.ai/chat/x?a=1#b", "https://claude.ai/chat/x"),
        ("https://chatgpt.com/codex", "https://chatgpt.com/codex"),
        (None, None),
        ("", None),
    ],
)
def test_strip_url(raw, expected):
    assert browser_adapter.strip_url(raw) == expected


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


def test_opencode_keeps_the_attachment_count_and_message_id():
    """The plugin sends both (hook-specs.md §5); dropping them loses image-only turns."""
    parsed = oc.parse(
        {
            "session_id": "ses_1",
            "prompt": "look at this",
            "messageID": "msg-7",
            "attachments": 2,
        }
    )
    assert parsed.metadata["messageID"] == "msg-7"
    assert parsed.metadata["attachments"] == 2
    assert isinstance(parsed.metadata["attachments"], int)


def test_opencode_attachment_count_of_zero_is_recorded():
    parsed = oc.parse({"session_id": "s", "prompt": "hi", "attachments": 0})
    assert parsed.metadata["attachments"] == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(["a", "b"], 2), ("3", 3), (None, None), ("many", None), (True, None), (-1, None)],
)
def test_opencode_attachment_count_coercion(value, expected):
    parsed = oc.parse({"session_id": "s", "prompt": "hi", "attachments": value})
    assert parsed.metadata.get("attachments") == expected


@pytest.mark.parametrize("payload", [None, [], "text", 7, 1.5, True, ()])
def test_every_adapter_rejects_non_dict_payloads(payload):
    for parse in (cc.parse, codex_adapter.parse, oc.parse):
        with pytest.raises(AdapterError):
            parse(payload)
    with pytest.raises(AdapterError):
        browser_adapter.parse(payload, source=Source.CLAUDE_WEB)


def test_adapters_tolerate_an_enormous_prompt():
    """Capping is ingest's job, but no adapter may choke on the size."""
    huge = "x" * 500_000
    assert len(cc.parse({"hook_event_name": "UserPromptSubmit", "prompt": huge}).prompt)
    assert len(oc.parse({"prompt": huge, "session_id": "s"}).prompt) == 500_000


#: Verbatim JSON emitted by opencode-plugin/agent-prompt-capture.js, captured by
#: driving the real plugin under node with a stub `apc` on PATH.
OPENCODE_PLUGIN_PAYLOAD = {
    "session_id": "sess-1",
    "cwd": "/Users/alice/dev/proj",
    "project": "my-project",
    "model": "anthropic/claude-opus-5",
    "agent": "build",
    "prompt": "real user text",
    "ts": "2025-09-19T20:05:45.678Z",
    "messageID": "msg-1",
    "attachments": 1,
}

OPENCODE_PLUGIN_TURN_END = {
    "event": "turn_end",
    "session_id": "sess-1",
    "ts": "2026-09-19T21:10:52.985Z",
}


def test_the_real_plugin_payload_round_trips():
    parsed = oc.parse(OPENCODE_PLUGIN_PAYLOAD)
    assert isinstance(parsed, RawPrompt)
    assert parsed.prompt == "real user text"
    assert parsed.session_id == "sess-1"
    assert parsed.cwd == "/Users/alice/dev/proj"
    assert parsed.project == "my-project"
    assert parsed.ts == "2025-09-19T20:05:45.678Z"
    assert parsed.metadata == {
        "model": "anthropic/claude-opus-5",
        "agent": "build",
        "messageID": "msg-1",
        "attachments": 1,
    }


def test_the_real_plugin_turn_end_round_trips():
    parsed = oc.parse(OPENCODE_PLUGIN_TURN_END)
    assert isinstance(parsed, RawTurnEnd)
    assert parsed.session_id == "sess-1"
    assert parsed.ts == "2026-09-19T21:10:52.985Z"
