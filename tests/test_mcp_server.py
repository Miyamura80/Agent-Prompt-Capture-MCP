"""MCP server: tool listing, one call per tool, and the resources.

The SDK renamed ``FastMCP`` to ``MCPServer`` in mcp 2.x and replaced
``create_connected_server_and_client_session`` with an in-process ``Client``; both
helpers are resolved dynamically so the tests track whichever SDK is installed.
"""

from __future__ import annotations

import json

import pytest
from helpers import make_record

from agent_prompt_capture.config import Config
from agent_prompt_capture.mcp_server import RESOURCE_URIS, TOOL_NAMES, build_server

pytestmark = pytest.mark.asyncio


def _client(server):
    """The in-memory client for the installed SDK version."""
    try:
        from mcp import Client  # mcp >= 2

        return Client(server)
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server._mcp_server)


@pytest.fixture
def mcp(apc_home, store):
    config = Config(home=apc_home, idle_gap_minutes=30.0, tail_minutes=5.0)
    for index, (prompt, ts, end, project) in enumerate(
        [
            ("refactor the payment module", "09:00", "09:04", "alpha"),
            ("add retries with backoff", "09:10", "09:14", "alpha"),
            ("write the parser tests", "09:20", "09:26", "beta"),
        ]
    ):
        store.insert(
            make_record(
                prompt,
                ts=f"2026-09-19T{ts}:00.000Z",
                turn_end_ts=f"2026-09-19T{end}:00.000Z",
                project=project,
                session_id="s1",
                id=f"rec-{index}",
            )
        )
    return {"server": build_server(config, store), "store": store}


def _is_error(result) -> bool:
    """``is_error`` on mcp 2.x, ``isError`` on 1.x."""
    for attribute in ("is_error", "isError"):
        value = getattr(result, attribute, None)
        if value is not None:
            return bool(value)
    return False


def _payload(result):
    """A tool result as a Python object."""
    for attribute in ("structured_content", "structuredContent"):
        structured = getattr(result, attribute, None)
        if structured:
            return structured
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


async def test_lists_every_tool(mcp):
    async with _client(mcp["server"]) as client:
        listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    assert names == set(TOOL_NAMES)
    for tool in listed.tools:
        assert tool.description


async def test_lists_every_resource(mcp):
    async with _client(mcp["server"]) as client:
        listed = await client.list_resources()
    assert {str(r.uri) for r in listed.resources} == set(RESOURCE_URIS)


async def test_list_prompts(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("list_prompts", {"limit": 2}))
    assert payload["total"] == 3
    assert len(payload["prompts"]) == 2
    assert payload["prompts"][0]["source"] == "claude_code"


async def test_list_prompts_filters(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("list_prompts", {"project": "beta"}))
    assert payload["total"] == 1
    assert payload["prompts"][0]["project"] == "beta"


async def test_list_prompts_rejects_an_unknown_source(mcp):
    async with _client(mcp["server"]) as client:
        result = await client.call_tool("list_prompts", {"source": "banana"})
    assert _is_error(result)


async def test_search_prompts(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("search_prompts", {"query": "payment"}))
    assert payload["count"] == 1
    assert "rank" in payload["results"][0]


async def test_get_prompt(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("get_prompt", {"id": "rec-0"}))
        missing = _payload(await client.call_tool("get_prompt", {"id": "nope"}))
    assert payload["id"] == "rec-0"
    assert missing["error"] == "not_found"


async def test_prompt_stats(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("prompt_stats", {"group_by": "project"}))
    keys = {g["key"]: g["prompt_count"] for g in payload["groups"]}
    assert keys == {"alpha": 2, "beta": 1}


async def test_list_sources(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("list_sources", {}))
    assert payload["sources"][0]["source"] == "claude_code"
    assert payload["sources"][0]["prompt_count"] == 3


async def test_list_sessions(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("list_sessions", {}))
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["sessions"][0]["prompt_count"] == 3


async def test_time_summary(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(
            await client.call_tool(
                "time_summary", {"since": "2026-09-19T00:00:00Z", "group_by": "project"}
            )
        )
    assert payload["total_active_minutes"] == 31.0
    assert payload["context_switches"] == 1
    assert {g["key"] for g in payload["groups"]} == {"alpha", "beta"}


async def test_activity_timeline(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(
            await client.call_tool(
                "activity_timeline",
                {"since": "2026-09-19T00:00:00Z", "until": "2026-09-19T23:00:00Z", "bucket": "day"},
            )
        )
    assert payload["bucket"] == "day"
    assert sum(b["prompt_count"] for b in payload["buckets"]) == 3


async def test_daily_digest(mcp):
    async with _client(mcp["server"]) as client:
        payload = _payload(await client.call_tool("daily_digest", {"date": "2026-09-19"}))
    assert payload["date"] == "2026-09-19"
    assert payload["prompt_count"] == 3
    assert payload["context_switches"] == 1


async def test_every_tool_is_callable_once(mcp):
    """One call per tool, with only its defaulted arguments."""
    arguments = {
        "list_prompts": {},
        "search_prompts": {"query": "payment"},
        "get_prompt": {"id": "rec-0"},
        "prompt_stats": {},
        "list_sources": {},
        "list_sessions": {},
        "time_summary": {},
        "activity_timeline": {},
        "daily_digest": {},
    }
    assert set(arguments) == set(TOOL_NAMES)
    async with _client(mcp["server"]) as client:
        for name, args in arguments.items():
            result = await client.call_tool(name, args)
            assert not _is_error(result), f"{name} failed: {result.content}"
            assert _payload(result) is not None


@pytest.mark.parametrize("uri", RESOURCE_URIS)
async def test_resources_return_json(mcp, uri):
    async with _client(mcp["server"]) as client:
        result = await client.read_resource(uri)
    text = result.contents[0].text
    assert isinstance(json.loads(text), dict)
