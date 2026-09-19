"""Store: schema, insert/dedup, search, stats, sessions, delete, turn ends."""

from __future__ import annotations

import pytest
from helpers import make_record

from agent_prompt_capture.models import Source
from agent_prompt_capture.store import SCHEMA_VERSION, Store


def test_schema_version_and_wal(store):
    assert store.schema_version == SCHEMA_VERSION
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_reopen_is_idempotent(apc_home):
    first = Store(apc_home / "prompts.db")
    first.insert(make_record("hello"))
    first.close()
    second = Store(apc_home / "prompts.db")
    assert second.count() == 1
    assert second.schema_version == SCHEMA_VERSION
    second.close()


def test_migration_adds_turn_end_column(apc_home):
    import sqlite3

    path = apc_home / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE prompts (id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,"
        " prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL, session_id TEXT, account TEXT,"
        " cwd TEXT, project TEXT, char_count INTEGER NOT NULL,"
        " pii_findings TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}');"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(prompts)")}
    assert "turn_end_ts" in columns
    store.close()


def test_insert_and_get(store):
    record = make_record("first prompt")
    assert store.insert(record) is True
    fetched = store.get(record.id)
    assert fetched is not None
    assert fetched.prompt == "first prompt"
    assert fetched.source is Source.CLAUDE_CODE
    assert store.get("nope") is None


def test_dedup_within_five_seconds(store):
    first = make_record("same", ts="2026-09-19T10:00:00.000Z")
    second = make_record("same", ts="2026-09-19T10:00:04.000Z")
    assert store.insert(first) is True
    assert store.insert(second) is False
    assert store.count() == 1


def test_dedup_window_expires(store):
    assert store.insert(make_record("same", ts="2026-09-19T10:00:00.000Z")) is True
    assert store.insert(make_record("same", ts="2026-09-19T10:00:06.000Z")) is True
    assert store.count() == 2


def test_dedup_is_per_session_and_source(store):
    base = "2026-09-19T10:00:00.000Z"
    assert store.insert(make_record("same", ts=base, session_id="a")) is True
    assert store.insert(make_record("same", ts=base, session_id="b")) is True
    assert store.insert(make_record("same", ts=base, source=Source.OPENCODE)) is True
    assert store.count() == 3


def test_dedup_with_null_session(store):
    base = "2026-09-19T10:00:00.000Z"
    assert store.insert(make_record("x", ts=base, session_id=None)) is True
    assert store.insert(make_record("x", ts=base, session_id=None)) is False


def test_list_filters_and_order(store):
    store.insert(make_record("a", ts="2026-09-19T10:00:00.000Z", project="alpha"))
    store.insert(make_record("b", ts="2026-09-19T11:00:00.000Z", project="beta"))
    store.insert(
        make_record("c", ts="2026-09-19T12:00:00.000Z", source=Source.OPENCODE, project="alpha")
    )

    assert [r.prompt for r in store.list()] == ["c", "b", "a"]
    assert [r.prompt for r in store.list(order="asc")] == ["a", "b", "c"]
    assert [r.prompt for r in store.list(source=Source.OPENCODE)] == ["c"]
    assert [r.prompt for r in store.list(project="alpha")] == ["c", "a"]
    assert [r.prompt for r in store.list(limit=1)] == ["c"]
    assert [r.prompt for r in store.list(limit=1, offset=1)] == ["b"]
    assert [r.prompt for r in store.list(since="2026-09-19T11:00:00Z")] == ["c", "b"]
    assert [r.prompt for r in store.list(until="2026-09-19T11:00:00Z")] == ["b", "a"]


def test_list_relative_since(store):
    from agent_prompt_capture.models import utc_now_iso

    store.insert(make_record("recent", ts=utc_now_iso()))
    store.insert(make_record("old", ts="2020-01-01T00:00:00.000Z", session_id="old"))
    assert [r.prompt for r in store.list(since="24h")] == ["recent"]
    assert [r.prompt for r in store.list(since="30m")] == ["recent"]
    assert len(store.list(since="7d")) == 1
    assert len(store.list(since="2w")) == 1


def test_count(store):
    store.insert(make_record("a", ts="2026-09-19T10:00:00.000Z"))
    store.insert(make_record("b", ts="2026-09-19T11:00:00.000Z"))
    assert store.count() == 2
    assert store.count(since="2026-09-19T10:30:00Z") == 1


def test_search(store):
    store.insert(make_record("refactor the payment module", ts="2026-09-19T10:00:00.000Z"))
    store.insert(make_record("write docs for the parser", ts="2026-09-19T11:00:00.000Z"))
    hits = store.search("payment")
    assert len(hits) == 1
    assert hits[0][0].prompt.startswith("refactor")
    assert isinstance(hits[0][1], float)
    assert store.search("nonexistentterm") == []
    assert store.search("") == []


def test_search_filters(store):
    store.insert(make_record("payment bug", ts="2026-09-19T10:00:00.000Z"))
    store.insert(make_record("payment bug", ts="2026-09-19T11:00:00.000Z", source=Source.OPENCODE))
    assert len(store.search("payment")) == 2
    assert len(store.search("payment", source=Source.OPENCODE)) == 1
    assert len(store.search("payment", since="2026-09-19T10:30:00Z")) == 1


def test_search_tolerates_fts_syntax_errors(store):
    """A query FTS5 cannot parse is retried quoted instead of raising."""
    store.insert(make_record("hello world"))
    assert store.search("hello OR world")
    assert isinstance(store.search('hello "unbalanced'), list)
    assert isinstance(store.search("((("), list)


def test_search_index_follows_deletes(store):
    record = make_record("uniqueneedle here")
    store.insert(record)
    assert len(store.search("uniqueneedle")) == 1
    store.delete(ids=[record.id])
    assert store.search("uniqueneedle") == []


@pytest.mark.parametrize("group_by", ["source", "day", "week", "project", "account", "session"])
def test_stats_group_by(store, group_by):
    store.insert(make_record("a", ts="2026-09-19T10:00:00.000Z", project="alpha", account="work"))
    store.insert(
        make_record(
            "b",
            ts="2026-09-20T10:00:00.000Z",
            project="beta",
            account="work",
            source=Source.OPENCODE,
            session_id="s2",
        )
    )
    rows = store.stats(group_by=group_by)
    assert rows
    assert sum(r["prompt_count"] for r in rows) == 2
    assert all({"key", "prompt_count", "chars"} <= set(r) for r in rows)


def test_stats_rejects_bad_group(store):
    with pytest.raises(ValueError, match="group_by"):
        store.stats(group_by="banana")


def test_stats_respects_time_window(store):
    store.insert(make_record("a", ts="2026-09-19T10:00:00.000Z"))
    store.insert(make_record("b", ts="2026-09-20T10:00:00.000Z"))
    rows = store.stats(since="2026-09-20T00:00:00Z", group_by="day")
    assert [r["key"] for r in rows] == ["2026-09-20"]


def test_sessions_and_sources(store):
    store.insert(make_record("a", ts="2026-09-19T10:00:00.000Z", session_id="s1"))
    store.insert(make_record("b", ts="2026-09-19T10:30:00.000Z", session_id="s1"))
    store.insert(
        make_record("c", ts="2026-09-19T11:00:00.000Z", session_id="s2", source=Source.OPENCODE)
    )

    sessions = store.sessions()
    assert {s["session_id"] for s in sessions} == {"s1", "s2"}
    s1 = next(s for s in sessions if s["session_id"] == "s1")
    assert s1["prompt_count"] == 2
    assert s1["first_ts"] == "2026-09-19T10:00:00.000Z"

    assert len(store.sessions(source=Source.OPENCODE)) == 1

    sources = store.sources()
    assert {s["source"] for s in sources} == {"claude_code", "opencode"}


def test_sessions_skip_null_ids(store):
    store.insert(make_record("a", session_id=None))
    assert store.sessions() == []


def test_delete_variants(store):
    a = make_record("a", ts="2026-09-19T10:00:00.000Z")
    b = make_record("b", ts="2026-09-20T10:00:00.000Z", source=Source.OPENCODE)
    store.insert(a)
    store.insert(b)

    assert store.delete() == 0
    assert store.delete(ids=[a.id]) == 1
    assert store.delete(source=Source.OPENCODE) == 1
    assert store.count() == 0


def test_delete_before(store):
    store.insert(make_record("old", ts="2026-09-01T10:00:00.000Z"))
    store.insert(make_record("new", ts="2026-09-20T10:00:00.000Z"))
    assert store.delete(before="2026-09-10T00:00:00Z") == 1
    assert [r.prompt for r in store.list()] == ["new"]


# ---------------------------------------------------------------------------
# turn ends
# ---------------------------------------------------------------------------


def test_mark_turn_end_sets_latest_open_prompt(store):
    first = make_record("a", ts="2026-09-19T10:00:00.000Z", session_id="s1")
    second = make_record("b", ts="2026-09-19T10:05:00.000Z", session_id="s1")
    store.insert(first)
    store.insert(second)

    updated = store.mark_turn_end(Source.CLAUDE_CODE, "s1", "2026-09-19T10:06:30.000Z")
    assert updated == second.id
    assert store.get(second.id).turn_end_ts == "2026-09-19T10:06:30.000Z"
    assert store.get(first.id).turn_end_ts is None


def test_mark_turn_end_walks_backwards(store):
    first = make_record("a", ts="2026-09-19T10:00:00.000Z", session_id="s1")
    second = make_record("b", ts="2026-09-19T10:05:00.000Z", session_id="s1")
    store.insert(first)
    store.insert(second)
    store.mark_turn_end(Source.CLAUDE_CODE, "s1", "2026-09-19T10:06:00.000Z")
    store.mark_turn_end(Source.CLAUDE_CODE, "s1", "2026-09-19T10:07:00.000Z")
    assert store.get(first.id).turn_end_ts == "2026-09-19T10:07:00.000Z"


def test_mark_turn_end_noop_when_nothing_open(store):
    assert store.mark_turn_end(Source.CLAUDE_CODE, "missing", "2026-09-19T10:00:00.000Z") is None
    assert store.mark_turn_end(Source.CLAUDE_CODE, None) is None


def test_mark_turn_end_is_source_scoped(store):
    record = make_record("a", session_id="s1", source=Source.OPENCODE)
    store.insert(record)
    assert store.mark_turn_end(Source.CLAUDE_CODE, "s1") is None
    assert store.mark_turn_end(Source.OPENCODE, "s1") == record.id


def test_mark_turn_end_defaults_to_now(store):
    record = make_record("a", session_id="s1")
    store.insert(record)
    store.mark_turn_end("claude_code", "s1")
    assert store.get(record.id).turn_end_ts is not None


def test_sessions_use_turn_end_for_last_ts(store):
    store.insert(
        make_record(
            "a",
            ts="2026-09-19T10:00:00.000Z",
            session_id="s1",
            turn_end_ts="2026-09-19T10:20:00.000Z",
        )
    )
    assert store.sessions()[0]["last_ts"] == "2026-09-19T10:20:00.000Z"
