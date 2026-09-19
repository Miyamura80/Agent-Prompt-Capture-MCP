"""``apc mcp``: the stdio MCP server.

Built on the official ``mcp`` Python SDK. Nothing here writes to stdout except the
MCP frames themselves; everything else goes to ``$APC_HOME/apc.log``.
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .config import Config, load_config, setup_logging
from .models import Source
from .store import Store
from .timeline import activity_timeline, daily_digest, time_summary

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

RESOURCE_URIS = (
    "apc://prompts/recent",
    "apc://stats/summary",
    "apc://digest/today",
)

INSTRUCTIONS = (
    "Local archive of the prompts this user has sent to their coding agents and chat UIs. "
    "Use time_summary for questions about where their time goes, activity_timeline for "
    "when they worked, daily_digest for a single day, and search_prompts/list_prompts to "
    "read what they actually asked. Prompt text is already PII-scrubbed."
)


def _server_class():
    """``FastMCP`` on mcp 1.x, ``MCPServer`` on mcp 2.x (same decorator API)."""
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415

        return FastMCP
    except ImportError:
        from mcp.server.mcpserver import MCPServer  # noqa: PLC0415

        return MCPServer


def _normalise_source(source: str | None) -> Source | None:
    if not source:
        return None
    try:
        return Source(source)
    except ValueError as exc:
        raise ValueError(f"unknown source {source!r}") from exc


def build_server(config: Config | None = None, store: Store | None = None) -> Any:
    """Create the MCP server with every tool and resource attached."""
    cfg = config or load_config()
    db = store if store is not None else Store(cfg.db_path)

    server_cls = _server_class()
    server = server_cls(
        name="agent-prompt-capture",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    @server.tool(description="List captured prompts, newest first, with optional filters.")
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
        src = _normalise_source(source)
        records = db.list(
            source=src,
            since=since,
            until=until,
            project=project,
            session_id=session_id,
            account=account,
            limit=limit,
            offset=offset,
        )
        total = db.count(
            source=src,
            since=since,
            until=until,
            project=project,
            session_id=session_id,
            account=account,
        )
        return {"prompts": [r.to_dict() for r in records], "total": total}

    @server.tool(description="Full text search over captured prompts (FTS5 syntax).")
    def search_prompts(
        query: str,
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        hits = db.search(
            query, source=_normalise_source(source), since=since, until=until, limit=limit
        )
        return {
            "results": [{**record.to_dict(), "rank": rank} for record, rank in hits],
            "count": len(hits),
        }

    @server.tool(description="Fetch one captured prompt by id.")
    def get_prompt(id: str) -> dict[str, Any]:  # noqa: A002 - contract name
        record = db.get(id)
        if record is None:
            return {"error": "not_found", "id": id}
        return record.to_dict()

    @server.tool(description="Cheap prompt counts grouped by source/day/week/project/account.")
    def prompt_stats(
        since: str | None = None,
        until: str | None = None,
        group_by: str = "source",
    ) -> dict[str, Any]:
        rows = db.stats(since=since, until=until, group_by=group_by)
        return {
            "group_by": group_by,
            "groups": [
                {"key": r["key"], "prompt_count": r["prompt_count"], "chars": r["chars"]}
                for r in rows
            ],
        }

    @server.tool(description="Which sources have captured prompts, and over what period.")
    def list_sources() -> dict[str, Any]:
        return {"sources": db.sources()}

    @server.tool(description="List agent/conversation sessions, most recent first.")
    def list_sessions(source: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"sessions": db.sessions(source=_normalise_source(source), limit=limit)}

    @server.tool(
        name="time_summary",
        description=(
            "Where the user's time went: active minutes, prompt counts, agent time and "
            "thinking time, grouped by project/source/day/hour_of_day/weekday/session. "
            "This is the tool to reach for when asked about time use."
        ),
    )
    def time_summary_tool(
        since: str = "7d",
        until: str | None = None,
        group_by: str = "project",
    ) -> dict[str, Any]:
        return time_summary(db, config=cfg, since=since, until=until, group_by=group_by)

    @server.tool(
        name="activity_timeline",
        description="Prompt volume and active minutes bucketed by hour or day.",
    )
    def activity_timeline_tool(
        since: str = "24h",
        until: str | None = None,
        bucket: str = "hour",
    ) -> dict[str, Any]:
        return activity_timeline(db, config=cfg, since=since, until=until, bucket=bucket)

    @server.tool(
        name="daily_digest",
        description="A single local day: activity sessions, context switches and top terms.",
    )
    def daily_digest_tool(date: str | None = None) -> dict[str, Any]:
        return daily_digest(db, config=cfg, date=date)

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
