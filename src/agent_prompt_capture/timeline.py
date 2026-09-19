"""Derived activity sessions, context switches and the time-tracking queries.

Nothing here is persisted: activity sessions are recomputed on every read from the
prompt timestamps and their ``turn_end_ts``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from typing import Any

from .config import Config
from .models import PromptRecord
from .store import Store
from .timeutil import parse_dt, parse_time, to_iso

__all__ = [
    "ActivitySession",
    "build_activity_sessions",
    "count_context_switches",
    "time_summary",
    "activity_timeline",
    "daily_digest",
    "top_terms",
    "TIME_GROUP_BY",
    "BUCKETS",
    "STOPWORDS",
]

TIME_GROUP_BY = ("project", "source", "day", "hour_of_day", "weekday", "session")
BUCKETS = ("hour", "day")

NO_PROJECT = "(none)"

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]*\]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{3,}")

STOPWORDS: frozenset[str] = frozenset(
    """
    about after also because been before being between both cannot could does doing down
    during each else even ever every from have here into just like make many more most
    much must need only other over please same should since some such than that their them
    then there these they thing think this those through very want were what when where
    which while will with would your yours user users make sure file files code line lines
    function functions using used also into onto back down does didn't dont doesnt have has
    had were was you're theyre lets let's okay yeah need needs needed
    """.split()
)


# --------------------------------------------------------------------------
# activity sessions
# --------------------------------------------------------------------------


@dataclass
class ActivitySession:
    """A stretch of work with no gap longer than ``idle_gap_minutes``."""

    key: str
    source: str | None
    project: str | None
    start: datetime
    end: datetime
    records: list[PromptRecord] = field(default_factory=list)
    tail_minutes: float = 0.0

    @property
    def prompt_count(self) -> int:
        return len(self.records)

    @property
    def active_minutes(self) -> float:
        span = (self.end - self.start).total_seconds() / 60.0
        return round(max(span, 0.0) + self.tail_minutes, 2)

    @property
    def interval(self) -> tuple[datetime, datetime]:
        """The wall-clock interval credited to this session (span + tail)."""
        return self.start, self.end + timedelta(minutes=self.tail_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "project": self.project,
            "start": to_iso(self.start),
            "end": to_iso(self.end),
            "prompt_count": self.prompt_count,
            "active_minutes": self.active_minutes,
        }


def _event_start(rec: PromptRecord) -> datetime | None:
    return parse_dt(rec.ts)


def _event_end(rec: PromptRecord) -> datetime | None:
    return parse_dt(rec.turn_end_ts) or parse_dt(rec.ts)


def agent_seconds(rec: PromptRecord) -> float | None:
    """How long the agent worked on this prompt, when we know."""
    start = parse_dt(rec.ts)
    end = parse_dt(rec.turn_end_ts)
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    return delta if delta >= 0 else None


def think_seconds(rec: PromptRecord, nxt: PromptRecord | None) -> float | None:
    """How long the user thought before the next prompt of the same session."""
    if nxt is None or rec.session_id is None or nxt.session_id != rec.session_id:
        return None
    end = parse_dt(rec.turn_end_ts)
    start = parse_dt(nxt.ts)
    if end is None or start is None:
        return None
    delta = (start - end).total_seconds()
    return delta if delta >= 0 else None


def _sorted(records: Iterable[PromptRecord]) -> list[PromptRecord]:
    return sorted(
        (r for r in records if parse_dt(r.ts) is not None),
        key=lambda r: (parse_dt(r.ts) or datetime.min.replace(tzinfo=UTC), r.id),
    )


def build_activity_sessions(
    records: Iterable[PromptRecord],
    *,
    idle_gap_minutes: float = 30.0,
    tail_minutes: float = 5.0,
    group_by: str | None = None,
) -> list[ActivitySession]:
    """Split a stream of prompts into activity sessions.

    ``group_by`` of ``"project"``/``"source"`` groups by ``(source, project)`` first,
    ``"session"`` groups by ``session_id``, anything else walks the whole stream.
    """
    ordered = _sorted(records)
    if not ordered:
        return []

    gap = timedelta(minutes=max(idle_gap_minutes, 0.0))

    if group_by in ("project", "source"):

        def bucket(rec: PromptRecord) -> tuple[Any, ...]:
            return (rec.source.value, rec.project or NO_PROJECT)
    elif group_by == "session":

        def bucket(rec: PromptRecord) -> tuple[Any, ...]:
            return (rec.session_id or "(none)",)
    else:

        def bucket(rec: PromptRecord) -> tuple[Any, ...]:
            return ()

    groups: dict[tuple[Any, ...], list[PromptRecord]] = {}
    for rec in ordered:
        groups.setdefault(bucket(rec), []).append(rec)

    sessions: list[ActivitySession] = []
    for group_key, group_records in groups.items():
        current: ActivitySession | None = None
        previous_end: datetime | None = None
        for rec in group_records:
            start = _event_start(rec)
            end = _event_end(rec)
            if start is None or end is None:
                continue
            if current is None or previous_end is None or (start - previous_end) > gap:
                current = ActivitySession(
                    key=_session_key(group_by, group_key, rec),
                    source=rec.source.value,
                    project=rec.project,
                    start=start,
                    end=max(start, end),
                    tail_minutes=max(tail_minutes, 0.0),
                )
                sessions.append(current)
            current.records.append(rec)
            current.end = max(current.end, end, start)
            previous_end = max(end, previous_end or end)
        # keep the per-group walk isolated
    sessions.sort(key=lambda s: s.start)
    return sessions


def _session_key(group_by: str | None, group_key: tuple[Any, ...], rec: PromptRecord) -> str:
    if group_by == "project":
        return rec.project or NO_PROJECT
    if group_by == "source":
        return rec.source.value
    if group_by == "session":
        return str(group_key[0])
    return "all"


def count_context_switches(sessions: Sequence[ActivitySession]) -> int:
    """Consecutive prompt pairs inside one activity session with a different project."""
    switches = 0
    for session in sessions:
        ordered = _sorted(session.records)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (previous.project or NO_PROJECT) != (current.project or NO_PROJECT):
                switches += 1
    return switches


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _local_tz():
    return datetime.now().astimezone().tzinfo or UTC


def _overlap_minutes(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    seconds = (end - start).total_seconds()
    return seconds / 60.0 if seconds > 0 else 0.0


def _metrics(records: Sequence[PromptRecord]) -> tuple[float, float | None]:
    """(agent_minutes, avg_think_seconds) for a bag of records."""
    ordered = _sorted(records)
    by_session: dict[str | None, list[PromptRecord]] = {}
    for rec in ordered:
        by_session.setdefault(rec.session_id, []).append(rec)

    agent_total = 0.0
    thinks: list[float] = []
    for group in by_session.values():
        for index, rec in enumerate(group):
            seconds = agent_seconds(rec)
            if seconds is not None:
                agent_total += seconds
            nxt = group[index + 1] if index + 1 < len(group) else None
            value = think_seconds(rec, nxt)
            if value is not None:
                thinks.append(value)
    avg_think = round(sum(thinks) / len(thinks), 2) if thinks else None
    return round(agent_total / 60.0, 2), avg_think


def _fetch(
    store: Store,
    since: str | None,
    until: str | None,
    *,
    limit: int = 100_000,
) -> list[PromptRecord]:
    return store.list(since=since, until=until, limit=limit, order="asc")


# --------------------------------------------------------------------------
# queries behind the MCP tools
# --------------------------------------------------------------------------


def time_summary(
    store: Store,
    *,
    config: Config | None = None,
    since: str | None = "7d",
    until: str | None = None,
    group_by: str = "project",
) -> dict[str, Any]:
    """Where the time went, grouped as asked."""
    key = str(group_by or "project").lower()
    if key not in TIME_GROUP_BY:
        raise ValueError(f"group_by must be one of {', '.join(TIME_GROUP_BY)}, got {group_by!r}")

    cfg = config or Config()
    low = parse_time(since)
    high = parse_time(until)
    records = _fetch(store, low, high)

    global_sessions = build_activity_sessions(
        records,
        idle_gap_minutes=cfg.idle_gap_minutes,
        tail_minutes=cfg.tail_minutes,
    )
    switches = count_context_switches(global_sessions)
    total_active = round(sum(s.active_minutes for s in global_sessions), 2)

    groups: list[dict[str, Any]] = []

    if key in ("project", "source", "session"):
        sessions = build_activity_sessions(
            records,
            idle_gap_minutes=cfg.idle_gap_minutes,
            tail_minutes=cfg.tail_minutes,
            group_by=key,
        )
        buckets: dict[str, list[ActivitySession]] = {}
        for session in sessions:
            buckets.setdefault(session.key, []).append(session)
        for name, items in buckets.items():
            bag = [r for s in items for r in s.records]
            agent_minutes, avg_think = _metrics(bag)
            groups.append(
                {
                    "key": name,
                    "active_minutes": round(sum(s.active_minutes for s in items), 2),
                    "prompt_count": len(bag),
                    "agent_minutes": agent_minutes,
                    "avg_think_seconds": avg_think,
                }
            )
    else:
        tz = _local_tz()
        labellers = {
            "day": lambda dt: dt.strftime("%Y-%m-%d"),
            "hour_of_day": lambda dt: f"{dt.hour:02d}",
            "weekday": lambda dt: WEEKDAY_NAMES[dt.weekday()],
        }
        label = labellers[key]
        minutes: dict[str, float] = {}
        bags: dict[str, list[PromptRecord]] = {}
        for session in global_sessions:
            for slice_start, slice_minutes in _slice_by_label(session, tz, label):
                minutes[slice_start] = minutes.get(slice_start, 0.0) + slice_minutes
        for rec in records:
            moment = parse_dt(rec.ts)
            if moment is None:
                continue
            name = label(moment.astimezone(tz))
            bags.setdefault(name, []).append(rec)
            minutes.setdefault(name, 0.0)
        for name in sorted(minutes):
            bag = bags.get(name, [])
            agent_minutes, avg_think = _metrics(bag)
            groups.append(
                {
                    "key": name,
                    "active_minutes": round(minutes[name], 2),
                    "prompt_count": len(bag),
                    "agent_minutes": agent_minutes,
                    "avg_think_seconds": avg_think,
                }
            )

    groups.sort(key=lambda g: (-g["active_minutes"], str(g["key"])))
    return {
        "since": low,
        "until": high,
        "group_by": key,
        "groups": groups,
        "total_active_minutes": total_active,
        "context_switches": switches,
    }


def _slice_by_label(session: ActivitySession, tz, label) -> list[tuple[str, float]]:
    """Split a session's credited interval into per-label minute slices."""
    start, end = session.interval
    if end <= start:
        return [(label(start.astimezone(tz)), session.active_minutes)]
    out: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        local = cursor.astimezone(tz)
        hour_start = local.replace(minute=0, second=0, microsecond=0)
        boundary = (hour_start + timedelta(hours=1)).astimezone(UTC)
        step_end = min(boundary, end)
        out.append((label(local), (step_end - cursor).total_seconds() / 60.0))
        cursor = step_end
    merged: dict[str, float] = {}
    for name, value in out:
        merged[name] = merged.get(name, 0.0) + value
    return list(merged.items())


def activity_timeline(
    store: Store,
    *,
    config: Config | None = None,
    since: str | None = "24h",
    until: str | None = None,
    bucket: str = "hour",
) -> dict[str, Any]:
    """Prompt counts and active minutes bucketed by hour or day."""
    unit = str(bucket or "hour").lower()
    if unit not in BUCKETS:
        raise ValueError(f"bucket must be one of {', '.join(BUCKETS)}, got {bucket!r}")

    cfg = config or Config()
    low = parse_time(since) or parse_time("24h")
    high = parse_time(until) or to_iso(datetime.now(UTC))
    records = _fetch(store, low, high)
    sessions = build_activity_sessions(
        records, idle_gap_minutes=cfg.idle_gap_minutes, tail_minutes=cfg.tail_minutes
    )

    tz = _local_tz()
    start_dt = parse_dt(low) or datetime.now(UTC) - timedelta(days=1)
    end_dt = parse_dt(high) or datetime.now(UTC)
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)

    local_start = start_dt.astimezone(tz)
    if unit == "hour":
        local_start = local_start.replace(minute=0, second=0, microsecond=0)
    else:
        local_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)

    buckets: list[dict[str, Any]] = []
    cursor = local_start
    guard = 0
    while cursor.astimezone(UTC) < end_dt and guard < 5000:
        guard += 1
        window = (cursor.astimezone(UTC), (cursor + step).astimezone(UTC))
        in_window = [
            r
            for r in records
            if (moment := parse_dt(r.ts)) is not None and window[0] <= moment < window[1]
        ]
        minutes = sum(_overlap_minutes(s.interval, window) for s in sessions)
        buckets.append(
            {
                "start": to_iso(window[0]),
                "local_start": cursor.isoformat(),
                "prompt_count": len(in_window),
                "active_minutes": round(minutes, 2),
                "projects": sorted({r.project for r in in_window if r.project}),
                "sources": sorted({r.source.value for r in in_window}),
            }
        )
        cursor = cursor + step

    return {"since": low, "until": high, "bucket": unit, "buckets": buckets}


def top_terms(records: Iterable[PromptRecord], *, limit: int = 15) -> list[dict[str, Any]]:
    """The most common meaningful words across a bag of prompts."""
    counter: Counter[str] = Counter()
    for rec in records:
        text = _PLACEHOLDER_RE.sub(" ", rec.prompt or "")
        for token in _TOKEN_RE.findall(text.lower()):
            if len(token) >= 4 and token not in STOPWORDS:
                counter[token] += 1
    return [{"term": term, "count": count} for term, count in counter.most_common(limit)]


def daily_digest(
    store: Store,
    *,
    config: Config | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Everything worth knowing about one local calendar day."""
    cfg = config or Config()
    tz = _local_tz()
    if date:
        try:
            day = date_cls.fromisoformat(str(date))
        except ValueError as exc:
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}") from exc
    else:
        day = datetime.now(tz).date()

    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    low = to_iso(start_local.astimezone(UTC))
    high = to_iso(end_local.astimezone(UTC))

    records = _fetch(store, low, high)
    sessions = build_activity_sessions(
        records,
        idle_gap_minutes=cfg.idle_gap_minutes,
        tail_minutes=cfg.tail_minutes,
        group_by="project",
    )
    global_sessions = build_activity_sessions(
        records, idle_gap_minutes=cfg.idle_gap_minutes, tail_minutes=cfg.tail_minutes
    )

    session_dicts = []
    for session in sessions:
        samples = sorted(
            (r.prompt for r in session.records if r.prompt), key=lambda p: (len(p), p)
        )[:3]
        session_dicts.append(
            {
                "project": session.project,
                "source": session.source,
                "start": to_iso(session.start),
                "end": to_iso(session.end),
                "prompt_count": session.prompt_count,
                "active_minutes": session.active_minutes,
                "sample_prompts": samples,
            }
        )

    starts = [parse_dt(r.ts) for r in records]
    starts = [s for s in starts if s is not None]
    ends = [e for e in (_event_end(r) for r in records) if e is not None]

    return {
        "date": day.isoformat(),
        "first_activity": to_iso(min(starts)) if starts else None,
        "last_activity": to_iso(max(ends)) if ends else None,
        "active_minutes": round(sum(s.active_minutes for s in global_sessions), 2),
        "prompt_count": len(records),
        "sessions": session_dicts,
        "context_switches": count_context_switches(global_sessions),
        "top_terms": top_terms(records),
    }
