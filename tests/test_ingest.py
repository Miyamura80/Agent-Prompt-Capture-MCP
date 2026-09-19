"""Ingest: allowlist, disabled sources, scrubbing, turn ends, dedup."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_prompt_capture.adapters import AdapterError
from agent_prompt_capture.ingest import SOURCE_ALIASES, adapter_for, ingest
from agent_prompt_capture.models import Source
from agent_prompt_capture.timeutil import to_iso

CLAUDE = {
    "session_id": "abc123",
    "cwd": "/Users/alice/dev/proj",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "email alice@example.com about the parser",
}

BROWSER = {
    "source": "claude_web",
    "prompt": "hello from the browser",
    "account": "me@work.com",
    "conversation_id": "conv-1",
    "url": "https://claude.ai/chat/uuid?x=1#y",
    "title": "some title",
}

ALLOWLIST = """
[capture]
allowed_accounts = ["me@work.com"]

[accounts]
"me@work.com" = "work"
"""


def test_cli_prompt_is_scrubbed_and_stored(config, store):
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert record is not None
    assert "alice@example.com" not in record.prompt
    assert "[EMAIL_1]" in record.prompt
    assert record.pii_findings["email"] == 1
    assert record.cwd == "/Users/[USER]/dev/proj"
    assert record.project == "proj"
    assert record.char_count == len(record.prompt)
    assert store.get(record.id) is not None


def test_prompt_hash_is_of_the_raw_text(config, store):
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert record.prompt_hash == hashlib.sha256(CLAUDE["prompt"].encode()).hexdigest()


def test_raw_text_never_reaches_the_database(config, store):
    ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    blob = config.db_path.read_bytes()
    assert b"alice@example.com" not in blob


def test_cli_sources_ignore_the_allowlist(write_config, store):
    config = write_config(ALLOWLIST)
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert record is not None
    assert record.account is None


def test_browser_source_requires_an_allowed_account(write_config, store):
    config = write_config(ALLOWLIST)
    assert ingest(Source.CLAUDE_WEB, BROWSER, config=config, store=store) is not None
    denied = {**BROWSER, "account": "stranger@nope.com"}
    assert ingest(Source.CLAUDE_WEB, denied, config=config, store=store) is None
    assert store.count() == 1


def test_browser_source_without_account(write_config, store):
    config = write_config(ALLOWLIST)
    payload = {k: v for k, v in BROWSER.items() if k != "account"}
    assert ingest(Source.CLAUDE_WEB, payload, config=config, store=store) is None


def test_empty_allowlist_blocks_all_browser_capture(config, store):
    assert ingest(Source.CLAUDE_WEB, BROWSER, config=config, store=store) is None


def test_account_is_stored_as_an_alias(write_config, store):
    config = write_config(ALLOWLIST)
    record = ingest(Source.CLAUDE_WEB, BROWSER, config=config, store=store)
    assert record.account == "work"
    assert "me@work.com" not in config.db_path.read_bytes().decode("utf-8", "ignore")


def test_account_without_alias_is_hashed(write_config, store):
    config = write_config('[capture]\nallowed_accounts = ["me@work.com"]\n')
    record = ingest(Source.CLAUDE_WEB, BROWSER, config=config, store=store)
    assert record.account.startswith("sha256:")


def test_disabled_source_returns_none(write_config, store):
    config = write_config('[capture]\ndisabled_sources = ["claude_code"]\n')
    assert ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store) is None
    assert store.count() == 0


def test_metadata_is_scrubbed(config, store):
    payload = {
        **CLAUDE,
        "transcript_path": "/Users/alice/.claude/projects/x.jsonl",
        "permission_mode": "contact bob@example.com",
    }
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    assert "alice" not in record.metadata["transcript_path"]
    assert "bob@example.com" not in record.metadata["permission_mode"]


def test_project_is_scrubbed_against_extra_terms(write_config, store):
    config = write_config('[pii]\nextra_terms = ["acme"]\n')
    payload = {**CLAUDE, "cwd": "/Users/alice/dev/acme"}
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    assert record.project == "[USER_TERM_1]"


def test_extra_terms_apply_to_the_prompt(write_config, store):
    config = write_config('[pii]\nextra_terms = ["acme"]\n')
    payload = {**CLAUDE, "prompt": "ship the Acme launch"}
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    assert "Acme" not in record.prompt


def test_dedup_returns_none(config, store):
    first = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    second = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert first is not None
    assert second is None
    assert store.count() == 1


def test_empty_prompt_returns_none(config, store):
    assert (
        ingest(Source.CLAUDE_CODE, {**CLAUDE, "prompt": "  "}, config=config, store=store) is None
    )


def test_unknown_event_raises(config, store):
    with pytest.raises(AdapterError):
        ingest(
            Source.CLAUDE_CODE,
            {**CLAUDE, "hook_event_name": "PreToolUse"},
            config=config,
            store=store,
        )


def test_ts_defaults_to_now(config, store):
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert record.ts.endswith("Z")


def test_ts_from_payload_is_normalised(config, store):
    payload = {**CLAUDE, "ts": "2026-09-19T20:11:03.123Z"}
    # claude_code carries no ts in the contract, so use opencode
    record = ingest(
        Source.OPENCODE,
        {"prompt": "hi", "session_id": "s", "ts": "2026-09-19T20:11:03.123Z"},
        config=config,
        store=store,
    )
    assert record.ts == "2026-09-19T20:11:03.123Z"
    assert payload["ts"] == record.ts


def test_source_accepts_a_string(config, store):
    assert ingest("claude_code", CLAUDE, config=config, store=store) is not None


# ---------------------------------------------------------------------------
# turn ends
# ---------------------------------------------------------------------------


def test_claude_code_stop_marks_the_turn_end(config, store):
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    result = ingest(
        Source.CLAUDE_CODE,
        {"session_id": "abc123", "hook_event_name": "Stop", "ts": "2026-09-19T21:00:00.000Z"},
        config=config,
        store=store,
    )
    assert result is None
    assert store.get(record.id).turn_end_ts == "2026-09-19T21:00:00.000Z"


def test_opencode_turn_end_event(config, store):
    record = ingest(
        Source.OPENCODE,
        {"prompt": "do a thing", "session_id": "ses_1"},
        config=config,
        store=store,
    )
    ingest(
        Source.OPENCODE,
        {"event": "turn_end", "session_id": "ses_1", "ts": "2026-09-19T21:00:00.000Z"},
        config=config,
        store=store,
    )
    assert store.get(record.id).turn_end_ts == "2026-09-19T21:00:00.000Z"


def test_codex_turn_complete_stores_and_ends(config, store):
    record = ingest(
        Source.CODEX_CLI,
        {
            "type": "agent-turn-complete",
            "turn-id": "t1",
            "thread-id": "th1",
            "input-messages": ["first", "make it faster"],
            "last-assistant-message": "done",
            "ts": "2026-09-19T21:00:00.000Z",
        },
        config=config,
        store=store,
    )
    assert record is not None
    assert record.prompt == "make it faster"
    assert record.turn_end_ts == "2026-09-19T21:00:00.000Z"
    assert store.get(record.id).turn_end_ts == "2026-09-19T21:00:00.000Z"


def test_turn_end_for_unknown_session_is_a_noop(config, store):
    assert (
        ingest(
            Source.CLAUDE_CODE,
            {"session_id": "nothing", "hook_event_name": "Stop"},
            config=config,
            store=store,
        )
        is None
    )


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("claude-code", Source.CLAUDE_CODE),
        ("codex", Source.CODEX_CLI),
        ("opencode", Source.OPENCODE),
    ],
)
def test_source_aliases(alias, expected):
    assert SOURCE_ALIASES[alias] is expected


@pytest.mark.parametrize("source", list(Source))
def test_every_source_has_an_adapter(source):
    assert callable(adapter_for(source))


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


def test_a_huge_prompt_is_truncated_and_says_so(config, store):
    from agent_prompt_capture.ingest import MAX_PROMPT_CHARS

    payload = {**CLAUDE, "prompt": "x" * (MAX_PROMPT_CHARS + 5_000)}
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    assert record is not None
    assert record.char_count == MAX_PROMPT_CHARS
    assert record.metadata["prompt_truncated"] is True
    assert record.metadata["prompt_original_chars"] == MAX_PROMPT_CHARS + 5_000


def test_a_truncated_prompt_hashes_what_we_stored(config, store):
    from agent_prompt_capture.ingest import MAX_PROMPT_CHARS

    text = "y" * (MAX_PROMPT_CHARS + 10)
    record = ingest(Source.CLAUDE_CODE, {**CLAUDE, "prompt": text}, config=config, store=store)
    expected = hashlib.sha256(text[:MAX_PROMPT_CHARS].encode()).hexdigest()
    assert record.prompt_hash == expected


def test_a_normal_prompt_is_not_marked_truncated(config, store):
    record = ingest(Source.CLAUDE_CODE, CLAUDE, config=config, store=store)
    assert "prompt_truncated" not in record.metadata


def test_a_huge_metadata_string_is_capped(config, store):
    from agent_prompt_capture.ingest import MAX_METADATA_CHARS

    payload = {**CLAUDE, "transcript_path": "/Users/alice/" + ("z" * 100_000)}
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    assert len(record.metadata["transcript_path"]) <= MAX_METADATA_CHARS + 32


def test_every_metadata_string_is_scrubbed_including_nested_ones(config, store):
    payload = {
        **CLAUDE,
        "prompt": "hello",
        "agent_type": "mail alice@example.com now",
        "prompt_id": "ring +14155552671",
    }
    record = ingest(Source.CLAUDE_CODE, payload, config=config, store=store)
    blob = json.dumps(record.metadata)
    assert "alice@example.com" not in blob
    assert "+14155552671" not in blob


def test_a_future_client_timestamp_is_distrusted(write_config, store):
    """Browser payloads carry the browser's clock; a future ts poisons every window."""
    from agent_prompt_capture.timeutil import parse_dt

    config = write_config(ALLOWLIST)
    future = to_iso(datetime.now(UTC) + timedelta(days=3))
    record = ingest(Source.CLAUDE_WEB, {**BROWSER, "ts": future}, config=config, store=store)
    assert record is not None
    assert parse_dt(record.ts) <= datetime.now(UTC) + timedelta(seconds=5)
    assert record.metadata["client_clock_skew"] is True
    assert record.metadata["client_ts"] == future


def test_a_slightly_fast_client_clock_is_accepted(write_config, store):
    config = write_config(ALLOWLIST)
    soon = to_iso(datetime.now(UTC) + timedelta(seconds=30))
    record = ingest(Source.CLAUDE_WEB, {**BROWSER, "ts": soon}, config=config, store=store)
    assert record.ts == soon
    assert "client_clock_skew" not in record.metadata


def test_a_past_client_timestamp_is_preserved(write_config, store):
    """The extension replays a queue after an outage; those timestamps are real."""
    config = write_config(ALLOWLIST)
    past = to_iso(datetime.now(UTC) - timedelta(hours=6))
    record = ingest(Source.CLAUDE_WEB, {**BROWSER, "ts": past}, config=config, store=store)
    assert record.ts == past
    assert "client_clock_skew" not in record.metadata


def test_two_browser_fires_of_the_same_prompt_still_dedupe(write_config, store):
    config = write_config(ALLOWLIST)
    stamp = to_iso(datetime.now(UTC))
    payload = {**BROWSER, "ts": stamp}
    assert ingest(Source.CLAUDE_WEB, payload, config=config, store=store) is not None
    assert ingest(Source.CLAUDE_WEB, payload, config=config, store=store) is None


def test_a_uuid_conversation_id_survives_as_the_session_id(write_config, store):
    """A UUID's digits are card-shaped; mangling them corrupts every browser session."""
    config = write_config(ALLOWLIST)
    conversation = "11111111-2222-3333-4444-555555555555"
    record = ingest(
        Source.CLAUDE_WEB,
        {**BROWSER, "conversation_id": conversation},
        config=config,
        store=store,
    )
    assert record.session_id == conversation


def test_a_turn_end_before_the_prompt_is_clamped(config, store):
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "t1",
        "input-messages": ["do the thing"],
        "ts": "2026-09-19T10:00:00.000Z",
    }
    record = ingest(Source.CODEX_CLI, payload, config=config, store=store)
    assert record is not None
    assert record.turn_end_ts >= record.ts


@pytest.mark.parametrize("payload", [None, [], "a string", 42, True])
@pytest.mark.parametrize(
    "source",
    [Source.CLAUDE_CODE, Source.CODEX_CLI, Source.OPENCODE, Source.CLAUDE_WEB],
)
def test_every_adapter_rejects_a_non_object_payload(config, store, source, payload):
    with pytest.raises(AdapterError):
        ingest(source, payload, config=config, store=store)
