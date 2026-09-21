"""Parsing of the ``since``/``until`` values used across the store, CLI and MCP server."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

__all__ = [
    "parse_time",
    "parse_duration",
    "to_iso",
    "parse_dt",
    "RELATIVE_RE",
    "MAX_FUTURE_SKEW_SECONDS",
]

#: How far ahead of our own clock a client-supplied timestamp may be before we
#: distrust it. Browser and hook payloads carry the *client's* clock; a past value is
#: legitimate (queued POSTs are replayed), a future one is skew and poisons every
#: time-window query as well as the 5 s dedup window.
MAX_FUTURE_SKEW_SECONDS = 300.0

RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m|h|d|w|mo|y)$", re.IGNORECASE)

_UNIT_SECONDS: dict[str, float] = {
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
    "mo": 2592000.0,
    "y": 31536000.0,
}


def parse_duration(value: str) -> timedelta:
    """``"24h"``/``"7d"``/``"2w"``/``"30m"`` -> a :class:`timedelta`."""
    match = RELATIVE_RE.match(value.strip())
    if not match:
        raise ValueError(f"not a relative duration: {value!r}")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    try:
        return timedelta(seconds=amount * _UNIT_SECONDS[unit])
    except (OverflowError, ValueError) as exc:
        # ``float`` saturates to inf and ``timedelta`` caps at ~999999999 days: an
        # oversized bound is a bad value, not an internal error.
        raise ValueError(f"duration out of range: {value!r}") from exc


def to_iso(dt: datetime) -> str:
    """Render an aware datetime the same way :func:`models.utc_now_iso` does."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_time(value: str | datetime | None, *, now: datetime | None = None) -> str | None:
    """Normalise a time bound to an ISO 8601 UTC string.

    Accepts ``None`` (passthrough), a :class:`datetime`, an ISO 8601 string
    (with or without a ``Z`` suffix), a bare date (``2026-09-19``), or a relative
    duration such as ``"24h"``, ``"7d"``, ``"2w"``, ``"30m"`` meaning *that long ago*.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return to_iso(dt)

    text = value.strip()
    if not text:
        return None

    reference = now or datetime.now(UTC)

    if text.lower() == "now":
        return to_iso(reference)

    if RELATIVE_RE.match(text):
        delta = parse_duration(text)
        try:
            return to_iso(reference - delta)
        except OverflowError as exc:  # a duration that walks off the datetime range
            raise ValueError(f"time out of range: {value!r}") from exc

    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"unrecognised time {value!r}: expected ISO 8601 or a relative form like '24h'"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return to_iso(dt)


def parse_dt(value: str | None) -> datetime | None:
    """Best-effort ISO 8601 -> aware UTC datetime. ``None`` when unparseable."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
