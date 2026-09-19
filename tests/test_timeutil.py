"""since/until parsing, shared by the store, CLI and MCP server."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_prompt_capture.timeutil import parse_dt, parse_duration, parse_time, to_iso

NOW = datetime(2026, 9, 19, 20, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("30m", 1800),
        ("24h", 86400),
        ("7d", 604800),
        ("2w", 1209600),
        ("1mo", 2592000),
        ("1y", 31536000),
        ("  12h  ", 43200),
        ("24H", 86400),
    ],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == timedelta(seconds=seconds)


@pytest.mark.parametrize("value", ["", "tomorrow", "24", "h", "-3d", "2026-09-19"])
def test_parse_duration_rejects(value):
    with pytest.raises(ValueError, match="relative duration"):
        parse_duration(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24h", "2026-09-18T20:00:00.000Z"),
        ("7d", "2026-09-12T20:00:00.000Z"),
        ("2w", "2026-09-05T20:00:00.000Z"),
        ("30m", "2026-09-19T19:30:00.000Z"),
        ("now", "2026-09-19T20:00:00.000Z"),
    ],
)
def test_parse_time_relative(value, expected):
    assert parse_time(value, now=NOW) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-19T20:11:03.123Z", "2026-09-19T20:11:03.123Z"),
        ("2026-09-19T20:11:03Z", "2026-09-19T20:11:03.000Z"),
        ("2026-09-19T20:11:03+00:00", "2026-09-19T20:11:03.000Z"),
        ("2026-09-19T22:11:03+02:00", "2026-09-19T20:11:03.000Z"),
        ("2026-09-19", "2026-09-19T00:00:00.000Z"),
        ("2026-09-19 20:11:03", "2026-09-19T20:11:03.000Z"),
    ],
)
def test_parse_time_iso(value, expected):
    assert parse_time(value) == expected


def test_parse_time_passthrough_and_datetime():
    assert parse_time(None) is None
    assert parse_time("") is None
    assert parse_time("   ") is None
    assert parse_time(NOW) == "2026-09-19T20:00:00.000Z"
    assert parse_time(datetime(2026, 9, 19, 20, 0)) == "2026-09-19T20:00:00.000Z"


def test_parse_time_rejects_nonsense():
    with pytest.raises(ValueError, match="unrecognised time"):
        parse_time("last tuesday")


def test_to_iso_round_trip():
    assert parse_dt(to_iso(NOW)) == NOW


@pytest.mark.parametrize("value", [None, "", "not a time"])
def test_parse_dt_is_forgiving(value):
    assert parse_dt(value) is None
