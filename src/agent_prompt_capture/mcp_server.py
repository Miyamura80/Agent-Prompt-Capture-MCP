"""``apc mcp``: the stdio MCP server.

Built on the official ``mcp`` Python SDK. Nothing here writes to stdout except the
MCP frames themselves; everything else goes to ``$APC_HOME/apc.log``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable
from typing import Any

from . import __version__
from .config import Config, load_config, setup_logging
from .models import Source
from .store import GROUP_BY_CHOICES, Store
from .timeline import BUCKETS, TIME_GROUP_BY, activity_timeline, daily_digest, time_summary

__all__ = ["build_server", "run", "TOOL_NAMES", "RESOURCE_URIS"]

TOOL_NAMES = (
    "list_prompts",
    "search_prompts",
    "get_prompt",
    "prompt_stats",
    "list_sources",
    "list_sessions",
    "time_summary",
    "activity_timeline",
    "daily_digest",
)

#: Upper bound on any tool's ``limit``: an unbounded list would blow the client's context.
MAX_LIMIT = 500

RESOURCE_URIS = (
    "apc://prompts/recent",
    "apc://stats/summary",
    "apc://digest/today",
)

SINCE_SYNTAX = (
    "`since`/`until` accept an ISO 8601 timestamp ('2026-09-19T14:00:00Z'), a bare date "
    "('2026-09-19'), the word 'now', or a relative duration meaning *that long ago*: "
    "'30m', '24h', '7d', '2w', '3mo', '1y'. Omit `until` for 'up to now'."
)

INSTRUCTIONS = f"""\
A local, read-only archive of the prompts this user has sent to their coding agents
(Claude Code, Codex CLI, OpenCode) and chat UIs (claude.ai, chatgpt.com), together with
when each turn started and when the agent finished it.

WHAT IT IS FOR
Help the user see where their time actually went, and suggest better ways to spend it:
which projects ate the week, when they do their best work, how often they were pulled
between projects, where they waited on an agent instead of working. Prefer concrete,
grounded observations over generic productivity advice - every number here comes from
their own captured activity, so quote it.

WHICH TOOL FOR WHICH QUESTION
- "where did my time go", "what did I work on this week", "which project took the most
  time", "when am I most active"  ->  time_summary (the main tool; group_by picks the
  axis: project, source, day, hour_of_day, weekday or session).
- "when was I working", "show me my day/week as a chart or a shape"  ->  activity_timeline
  (prompt volume and active minutes bucketed by hour or day).
- "what did I do today / on 2026-09-17", "summarise my day"  ->  daily_digest (one local
  calendar day: activity sessions, sample prompts, context switches, top terms).
- "what did I actually ask about X"  ->  search_prompts (SQLite FTS5 syntax) or
  list_prompts (filter by source/project/session/time and read them in order).
- "how many prompts", cheap counts only  ->  prompt_stats.
- "what is being captured at all"  ->  list_sources; "which conversations"  ->  list_sessions.
- One record by id  ->  get_prompt.

PRIVACY: THE PROMPT TEXT IS ALREADY SCRUBBED
Every prompt was PII-scrubbed before it was written to disk. Emails, phone numbers, API
keys, tokens, card numbers, home-directory usernames and similar values were replaced
with `[CATEGORY_N]` placeholders - `[EMAIL_1]`, `[API_KEY_2]`, `[HOME_PATH_1]` - where the
same placeholder means the same original value *within one prompt only*; numbering
restarts for every record, so `[EMAIL_1]` in two prompts need not be the same person. The
raw values are gone and cannot be recovered; `pii_findings` just counts what was replaced.
Treat placeholders as opaque, never guess what was behind one, and do not repeat a whole
prompt back to the user when a summary will do.

HOW TO READ THE TIME NUMBERS (ALL HEURISTIC)
- An *activity session* is a run of prompts with no gap longer than `idle_gap_minutes`
  (default 30) between the end of one turn and the start of the next.
- `active_minutes` = the span from a session's first prompt to its last turn end, plus a
  `tail_minutes` credit (default 5) for the work that follows the last prompt. It is time
  spent *around* the agent, not measured focus, and it is only credited while prompts are
  flowing: reading, meetings and thinking away from the keyboard are invisible.
- `agent_minutes` = the summed `turn_end_ts - ts` per prompt, i.e. how long the agent was
  working. It is NULL/0 for sources with no turn-end signal (all browser sources, and any
  turn the user interrupted), so a low number can mean "no signal", not "fast".
- "think time" (`avg_think_seconds`) = the gap between an agent finishing and the user's
  next prompt in the same session: how long they spent reading and deciding. Long think
  time is not automatically bad.
- A *context switch* is two consecutive prompts inside one activity session whose
  `project` differs. It counts jumps, not their cost.
- Hour-of-day, weekday and daily_digest use the **local timezone of this machine**;
  stored timestamps are UTC.
These are heuristics over prompt timestamps, not a time tracker. Say so when the answer
leans on them, and never present a derived minute count as measured fact.

TIME ARGUMENTS
{SINCE_SYNTAX}

Nothing here writes or deletes: to remove captured data the user runs `apc purge`.
"""


def _server_class():
    """``FastMCP`` on mcp 1.x, ``MCPServer`` on mcp 2.x (same decorator API)."""
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415

        return FastMCP
    except ImportError:
        from mcp.server.mcpserver import MCPServer  # noqa: PLC0415

        return MCPServer


SOURCE_VALUES: tuple[str, ...] = tuple(s.value for s in Source)


def _invalid(field: str, value: Any, choices: Iterable[str]) -> dict[str, Any]:
    """The structured error every tool returns instead of raising at the client."""
    valid = list(choices)
    return {
        "error": f"invalid {field}: {value!r}",
        "field": field,
        "valid_values": valid,
        "hint": f"{field} must be one of: {', '.join(valid)}",
    }


def _normalise_source(source: str | None) -> Source | None:
    """``None`` for "no filter". Raises :class:`ValueError` on an unknown source."""
    if not source:
        return None
    try:
        return Source(source)
    except ValueError as exc:
        raise ValueError(f"unknown source {source!r}") from exc


def _source_or_error(source: str | None) -> tuple[Source | None, dict[str, Any] | None]:
    try:
        return _normalise_source(source), None
    except ValueError:
        return None, _invalid("source", source, SOURCE_VALUES)


def _time_error(exc: ValueError) -> dict[str, Any]:
    return {"error": str(exc), "hint": SINCE_SYNTAX}


def _clamp(limit: Any, *, default: int = 50) -> int:
    """Keep an LLM-supplied ``limit`` inside something a client can actually read."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def build_server(config: Config | None = None, store: Store | None = None) -> Any:
    """Create the MCP server with every tool and resource attached."""
    cfg = config or load_config()
    db = store if store is not None else Store(cfg.db_path)

    server_cls = _server_class()
    kwargs: dict[str, Any] = {
        "name": "agent-prompt-capture",
        "instructions": INSTRUCTIONS,
    }
    if "version" in inspect.signature(server_cls.__init__).parameters:
        # mcp 2.x ``MCPServer`` advertises a server version; mcp 1.x ``FastMCP`` has no
        # such keyword and raises ``TypeError`` when one is passed.
        kwargs["version"] = __version__
    server = server_cls(**kwargs)

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    @server.tool(
        description=(
            "List captured prompts, newest first (oldest first is not offered; page with "
            "`offset`). Use it to read what the user actually asked, in order, about a "
            "project or a session. Prompt text is PII-scrubbed: values appear as "
            "[EMAIL_1], [API_KEY_2] and similar placeholders. "
            f"`source` must be one of: {', '.join(SOURCE_VALUES)}. "
            "`project`, `session_id` and `account` are exact matches on the stored value "
            "(see list_sessions / list_sources for what exists). "
            f"`limit` is capped at {MAX_LIMIT}. {SINCE_SYNTAX} "
            "Returns {prompts: [record...], total: <matching rows ignoring limit/offset>}."
        )
    )
    def list_prompts(
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        account: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        src, error = _source_or_error(source)
        if error:
            return error
        filters = {
            "source": src,
            "since": since,
            "until": until,
            "project": project,
            "session_id": session_id,
            "account": account,
        }
        try:
            records = db.list(**filters, limit=_clamp(limit), offset=max(int(offset or 0), 0))
            total = db.count(**filters)
        except ValueError as exc:
            return _time_error(exc)
        return {"prompts": [r.to_dict() for r in records], "total": total}

    @server.tool(
        description=(
            "Full text search over the captured prompt text, best match first. `query` is "
            'SQLite FTS5 syntax: bare words are AND-ed, "quoted phrases" match exactly, '
            "`foo OR bar`, `foo NOT bar` and `refact*` prefixes work. A query FTS5 cannot "
            "parse is retried as a literal phrase rather than failing. Remember the text is "
            "scrubbed, so searching for an email address or a key will never match. "
            f"`source` must be one of: {', '.join(SOURCE_VALUES)}. {SINCE_SYNTAX} "
            f"`limit` is capped at {MAX_LIMIT}. Each result carries a bm25 `rank` "
            "(lower = better)."
        )
    )
    def search_prompts(
        query: str,
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        src, error = _source_or_error(source)
        if error:
            return error
        try:
            hits = db.search(query, source=src, since=since, until=until, limit=_clamp(limit))
        except ValueError as exc:
            return _time_error(exc)
        return {
            "results": [{**record.to_dict(), "rank": rank} for record, rank in hits],
            "count": len(hits),
        }

    @server.tool(
        description=(
            "Fetch one captured prompt by its `id` (the `id` field of any record returned "
            "by list_prompts or search_prompts). Returns the record, or "
            '{"error": "not_found", "id": ...} when there is no such prompt.'
        )
    )
    def get_prompt(id: str) -> dict[str, Any]:  # noqa: A002 - contract name
        record = db.get(id)
        if record is None:
            return {"error": "not_found", "id": id}
        return record.to_dict()

    @server.tool(
        description=(
            "Cheap prompt counts and character totals - no time analysis. Reach for "
            "time_summary instead when the question is about time. "
            f"`group_by` must be one of: {', '.join(GROUP_BY_CHOICES)} "
            "('day' and 'week' bucket by the UTC timestamp, not local time). "
            f"{SINCE_SYNTAX} Returns {{group_by, groups: [{{key, prompt_count, chars}}]}}."
        )
    )
    def prompt_stats(
        since: str | None = None,
        until: str | None = None,
        group_by: str = "source",
    ) -> dict[str, Any]:
        if str(group_by).lower() not in GROUP_BY_CHOICES:
            return _invalid("group_by", group_by, GROUP_BY_CHOICES)
        try:
            rows = db.stats(since=since, until=until, group_by=group_by)
        except ValueError as exc:
            return _time_error(exc)
        return {
            "group_by": group_by,
            "groups": [
                {"key": r["key"], "prompt_count": r["prompt_count"], "chars": r["chars"]}
                for r in rows
            ],
        }

    @server.tool(
        description=(
            "Which capture sources have prompts, how many, and the first/last timestamp "
            "for each. Takes no arguments. Call it first when you are unsure what is being "
            "captured at all, or to check whether a source has gone quiet."
        )
    )
    def list_sources() -> dict[str, Any]:
        return {"sources": db.sources()}

    @server.tool(
        description=(
            "List agent/conversation sessions, most recently active first, with their "
            "project, first/last timestamp and prompt count. Use a returned `session_id` "
            "with list_prompts to read one conversation in order. "
            f"`source` must be one of: {', '.join(SOURCE_VALUES)}. "
            f"`limit` is capped at {MAX_LIMIT}."
        )
    )
    def list_sessions(source: str | None = None, limit: int = 50) -> dict[str, Any]:
        src, error = _source_or_error(source)
        if error:
            return error
        return {"sessions": db.sessions(source=src, limit=_clamp(limit))}

    @server.tool(
        name="time_summary",
        description=(
            "Where the user's time went. THE tool for any question about time use, focus "
            "or workload. Returns per-group `active_minutes` (wall-clock time around the "
            "agent, including a short tail credit), `prompt_count`, `agent_minutes` (how "
            "long agents were working; 0 where no turn-end signal exists, e.g. browser "
            "sources) and `avg_think_seconds` (the user's read-and-decide gap between an "
            "agent finishing and their next prompt), plus `total_active_minutes` and "
            "`context_switches` (consecutive prompts in one work session on different "
            "projects) for the whole window. "
            f"`group_by` must be one of: {', '.join(TIME_GROUP_BY)} - 'hour_of_day' and "
            "'weekday' use this machine's local timezone. "
            f"{SINCE_SYNTAX} Default `since` is '7d'. "
            "All figures are heuristics derived from prompt timestamps and idle gaps, not "
            "measured focus time; present them as such."
        ),
    )
    def time_summary_tool(
        since: str = "7d",
        until: str | None = None,
        group_by: str = "project",
    ) -> dict[str, Any]:
        if str(group_by or "").lower() not in TIME_GROUP_BY:
            return _invalid("group_by", group_by, TIME_GROUP_BY)
        try:
            return time_summary(db, config=cfg, since=since, until=until, group_by=group_by)
        except ValueError as exc:
            return _time_error(exc)

    @server.tool(
        name="activity_timeline",
        description=(
            "The shape of the user's activity over time: contiguous buckets, each with "
            "`start` (UTC), `local_start`, `prompt_count`, `active_minutes` and the "
            "`projects`/`sources` touched. Use it to show when work happened rather than "
            "how much. `bucket` must be one of: "
            f"{', '.join(BUCKETS)} - buckets are aligned to local hours/midnights. "
            f"{SINCE_SYNTAX} Defaults: since='24h', until=now. Very wide windows are "
            "truncated (the response then carries `truncated: true`); widen `bucket` "
            "instead of the window."
        ),
    )
    def activity_timeline_tool(
        since: str = "24h",
        until: str | None = None,
        bucket: str = "hour",
    ) -> dict[str, Any]:
        if str(bucket or "").lower() not in BUCKETS:
            return _invalid("bucket", bucket, BUCKETS)
        try:
            return activity_timeline(db, config=cfg, since=since, until=until, bucket=bucket)
        except ValueError as exc:
            return _time_error(exc)

    @server.tool(
        name="daily_digest",
        description=(
            "Everything worth knowing about one local calendar day: first/last activity, "
            "`active_minutes`, the day's activity sessions (project, source, start/end, "
            "prompt count and the three shortest prompts as samples), `context_switches` "
            "and `top_terms`. `date` is YYYY-MM-DD in this machine's local timezone and "
            "defaults to today. Use it for 'what did I do today?' and end-of-day reviews; "
            "use time_summary for anything spanning several days."
        ),
    )
    def daily_digest_tool(date: str | None = None) -> dict[str, Any]:
        try:
            return daily_digest(db, config=cfg, date=date)
        except ValueError as exc:
            return {"error": str(exc), "hint": "date must be YYYY-MM-DD, e.g. '2026-09-19'"}

    # ------------------------------------------------------------------
    # resources
    # ------------------------------------------------------------------

    @server.resource(
        "apc://prompts/recent",
        name="recent_prompts",
        description="The last 50 captured prompts as JSON.",
        mime_type="application/json",
    )
    def recent_prompts() -> str:
        return json.dumps(
            {"prompts": [r.to_dict() for r in db.list(limit=50)]}, indent=2, default=str
        )

    @server.resource(
        "apc://stats/summary",
        name="stats_summary",
        description="Per-source prompt counts for the last 7 days.",
        mime_type="application/json",
    )
    def stats_summary() -> str:
        return json.dumps(
            {"since": "7d", "groups": db.stats(since="7d", group_by="source")},
            indent=2,
            default=str,
        )

    @server.resource(
        "apc://digest/today",
        name="digest_today",
        description="Today's activity digest: sessions, context switches and top terms.",
        mime_type="application/json",
    )
    def digest_today() -> str:
        return json.dumps(daily_digest(db, config=cfg), indent=2, default=str)

    return server


def run(config: Config | None = None) -> None:
    """Entry point for ``apc mcp``."""
    cfg = config or load_config()
    setup_logging(cfg.home)
    server = build_server(cfg)
    server.run(transport="stdio")
