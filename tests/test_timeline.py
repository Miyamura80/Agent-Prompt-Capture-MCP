"""Activity-session heuristic, context switches and the three time queries.

The fixture below is hand-computed. All times are UTC on 2026-09-19, and the store is
loaded with ``idle_gap_minutes = 30`` and ``tail_minutes = 5``::

    #  ts       turn_end  project  session  gap from previous end
    1  09:00    09:04     alpha    s1       -
    2  09:10    09:14     alpha    s1       6 min   -> same activity session
    3  09:20    09:26     beta     s1       6 min   -> same session, CONTEXT SWITCH
    4  11:00    11:05     beta     s2       94 min  -> new activity session
    5  11:20    (open)    alpha    s2       15 min  -> same session, CONTEXT SWITCH

Global activity sessions:
    A: 09:00 -> 09:26  =  26 min + 5 tail = 31.0
    B: 11:00 -> 11:20  =  20 min + 5 tail = 25.0
    total active = 56.0, context switches = 2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers import make_record

from agent_prompt_capture.config import Config
from agent_prompt_capture.models import Source
from agent_prompt_capture.timeline import (
    activity_timeline,
    agent_seconds,
    build_activity_sessions,
    count_context_switches,
    daily_digest,
    time_summary,
    top_terms,
)
from agent_prompt_capture.timeutil import to_iso

DAY = "2026-09-19"


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00.000Z"


FIXTURE = [
    ("refactor the payment module please", "09:00", "09:04", "alpha", "s1"),
    ("add retries with backoff to payment", "09:10", "09:14", "alpha", "s1"),
    ("write the parser tests for beta", "09:20", "09:26", "beta", "s1"),
    ("fix the beta parser crash", "11:00", "11:05", "beta", "s2"),
    ("back to alpha billing work", "11:20", None, "alpha", "s2"),
]

EXPECTED_TOTAL_ACTIVE = 56.0
EXPECTED_SWITCHES = 2


@pytest.fixture
def cfg(apc_home):
    return Config(home=apc_home, idle_gap_minutes=30.0, tail_minutes=5.0)


@pytest.fixture
def loaded(store):
    for prompt, start, end, project, session in FIXTURE:
        store.insert(
            make_record(
                prompt,
                ts=at(start),
                turn_end_ts=at(end) if end else None,
                project=project,
                session_id=session,
            )
        )
    return store


@pytest.fixture
def records(loaded):
    return loaded.list(order="asc", limit=100)


# ---------------------------------------------------------------------------
# the heuristic itself
# ---------------------------------------------------------------------------


def test_gap_splits_activity_sessions(records):
    sessions = build_activity_sessions(records, idle_gap_minutes=30, tail_minutes=5)
    assert len(sessions) == 2
    first, second = sessions
    assert first.prompt_count == 3
    assert second.prompt_count == 2
    assert first.active_minutes == 31.0
    assert second.active_minutes == 25.0
    assert sum(s.active_minutes for s in sessions) == EXPECTED_TOTAL_ACTIVE


def test_tail_minutes_are_credited(records):
    without_tail = build_activity_sessions(records, idle_gap_minutes=30, tail_minutes=0)
    assert sum(s.active_minutes for s in without_tail) == EXPECTED_TOTAL_ACTIVE - 10.0


def test_a_tiny_gap_never_splits(records):
    sessions = build_activity_sessions(records, idle_gap_minutes=10_000, tail_minutes=0)
    assert len(sessions) == 1
    assert sessions[0].active_minutes == 140.0  # 09:00 -> 11:20


def test_a_zero_gap_splits_every_prompt(records):
    sessions = build_activity_sessions(records, idle_gap_minutes=0, tail_minutes=0)
    assert len(sessions) == len(FIXTURE)


def test_grouping_by_project_splits_the_streams(records):
    sessions = build_activity_sessions(
        records, idle_gap_minutes=30, tail_minutes=5, group_by="project"
    )
    keys = sorted(s.key for s in sessions)
    # alpha 09:00-09:14, beta 09:20-09:26, beta 11:00-11:05, alpha 11:20
    assert keys == ["alpha", "alpha", "beta", "beta"]


def test_context_switches(records):
    sessions = build_activity_sessions(records, idle_gap_minutes=30, tail_minutes=5)
    assert count_context_switches(sessions) == EXPECTED_SWITCHES


def test_no_context_switch_in_a_single_project(store):
    for index, start in enumerate(["09:00", "09:10", "09:20"]):
        store.insert(make_record(f"p{index}", ts=at(start), project="alpha"))
    sessions = build_activity_sessions(store.list(order="asc"), idle_gap_minutes=30)
    assert count_context_switches(sessions) == 0


def test_switch_across_activity_sessions_does_not_count(store):
    store.insert(make_record("a", ts=at("09:00"), project="alpha"))
    store.insert(make_record("b", ts=at("15:00"), project="beta"))
    sessions = build_activity_sessions(store.list(order="asc"), idle_gap_minutes=30)
    assert len(sessions) == 2
    assert count_context_switches(sessions) == 0


def test_empty_input():
    assert build_activity_sessions([]) == []
    assert count_context_switches([]) == 0


def test_agent_seconds(records):
    first = records[0]
    assert agent_seconds(first) == 240.0
    assert agent_seconds(records[-1]) is None


# ---------------------------------------------------------------------------
# time_summary
# ---------------------------------------------------------------------------


def test_time_summary_by_project(loaded, cfg):
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by="project")
    assert summary["total_active_minutes"] == EXPECTED_TOTAL_ACTIVE
    assert summary["context_switches"] == EXPECTED_SWITCHES
    keys = {g["key"]: g for g in summary["groups"]}
    assert set(keys) == {"alpha", "beta"}
    assert sum(g["prompt_count"] for g in summary["groups"]) == len(FIXTURE)
    # alpha: 09:00-09:14 (14+5) + 11:20 (0+5) = 24 minutes
    assert keys["alpha"]["active_minutes"] == 24.0
    # beta: 09:20-09:26 (6+5) + 11:00-11:05 (5+5) = 21 minutes
    assert keys["beta"]["active_minutes"] == 21.0


def test_time_summary_agent_and_think_minutes(loaded, cfg):
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by="project")
    keys = {g["key"]: g for g in summary["groups"]}
    # alpha agent time: 4 + 4 minutes (the last prompt is still open)
    assert keys["alpha"]["agent_minutes"] == 8.0
    # beta agent time: 6 + 5 minutes
    assert keys["beta"]["agent_minutes"] == 11.0
    # thinking inside s1: 09:04 -> 09:10 (360s) and 09:14 -> 09:20 (360s)
    assert keys["alpha"]["avg_think_seconds"] == 360.0


def test_time_summary_by_source(loaded, cfg):
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by="source")
    assert [g["key"] for g in summary["groups"]] == ["claude_code"]
    assert summary["groups"][0]["prompt_count"] == len(FIXTURE)


def test_time_summary_by_session(loaded, cfg):
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by="session")
    keys = {g["key"]: g["prompt_count"] for g in summary["groups"]}
    # The key is "<source>:<session_id>" - session ids are only unique per agent.
    assert keys == {"claude_code:s1": 3, "claude_code:s2": 2}


@pytest.mark.parametrize("group_by", ["day", "hour_of_day", "weekday"])
def test_time_summary_time_groupings(loaded, cfg, group_by):
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by=group_by)
    assert summary["groups"]
    assert sum(g["prompt_count"] for g in summary["groups"]) == len(FIXTURE)
    total = sum(g["active_minutes"] for g in summary["groups"])
    assert total == pytest.approx(EXPECTED_TOTAL_ACTIVE, abs=0.05)


def test_time_summary_rejects_bad_grouping(loaded, cfg):
    with pytest.raises(ValueError, match="group_by"):
        time_summary(loaded, config=cfg, group_by="banana")


def test_time_summary_empty_store(store, cfg):
    summary = time_summary(store, config=cfg, since="7d", group_by="project")
    assert summary["groups"] == []
    assert summary["total_active_minutes"] == 0
    assert summary["context_switches"] == 0


def test_time_summary_respects_the_window(loaded, cfg):
    summary = time_summary(
        loaded, config=cfg, since=at("10:00"), until=at("23:59"), group_by="project"
    )
    assert sum(g["prompt_count"] for g in summary["groups"]) == 2
    assert summary["total_active_minutes"] == 25.0


# ---------------------------------------------------------------------------
# activity_timeline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bucket", ["hour", "day"])
def test_activity_timeline_buckets(loaded, cfg, bucket):
    result = activity_timeline(
        loaded, config=cfg, since=at("00:00"), until=at("23:00"), bucket=bucket
    )
    assert result["bucket"] == bucket
    assert result["buckets"]
    assert sum(b["prompt_count"] for b in result["buckets"]) == len(FIXTURE)
    total = sum(b["active_minutes"] for b in result["buckets"])
    assert total == pytest.approx(EXPECTED_TOTAL_ACTIVE, abs=0.05)
    for entry in result["buckets"]:
        assert set(entry) >= {"start", "prompt_count", "active_minutes", "projects", "sources"}


def test_activity_timeline_lists_projects_and_sources(loaded, cfg):
    result = activity_timeline(
        loaded, config=cfg, since=at("00:00"), until=at("23:00"), bucket="day"
    )
    busy = [b for b in result["buckets"] if b["prompt_count"]]
    assert busy
    assert set(busy[0]["projects"]) == {"alpha", "beta"}
    assert busy[0]["sources"] == ["claude_code"]


def test_activity_timeline_rejects_a_bad_bucket(loaded, cfg):
    with pytest.raises(ValueError, match="bucket"):
        activity_timeline(loaded, config=cfg, bucket="fortnight")


def test_activity_timeline_on_an_empty_store(store, cfg):
    result = activity_timeline(store, config=cfg, since="2h", bucket="hour")
    assert all(b["prompt_count"] == 0 for b in result["buckets"])


# ---------------------------------------------------------------------------
# daily_digest
# ---------------------------------------------------------------------------


def test_daily_digest(loaded, cfg):
    digest = daily_digest(loaded, config=cfg, date=DAY)
    assert digest["date"] == DAY
    assert digest["prompt_count"] == len(FIXTURE)
    assert digest["context_switches"] == EXPECTED_SWITCHES
    assert digest["first_activity"] == at("09:00")
    assert digest["last_activity"] == at("11:20")
    assert digest["sessions"]
    for session in digest["sessions"]:
        assert set(session) >= {
            "project",
            "source",
            "start",
            "end",
            "prompt_count",
            "sample_prompts",
        }
        assert len(session["sample_prompts"]) <= 3


def test_daily_digest_samples_are_the_shortest(loaded, cfg):
    digest = daily_digest(loaded, config=cfg, date=DAY)
    alpha = [s for s in digest["sessions"] if s["project"] == "alpha"]
    samples = [p for s in alpha for p in s["sample_prompts"]]
    assert "back to alpha billing work" in samples


def test_daily_digest_top_terms(loaded, cfg):
    digest = daily_digest(loaded, config=cfg, date=DAY)
    terms = [t["term"] for t in digest["top_terms"]]
    assert "payment" in terms
    assert len(terms) <= 15
    assert all(len(t) >= 4 for t in terms)


def test_daily_digest_empty_day(loaded, cfg):
    digest = daily_digest(loaded, config=cfg, date="2020-01-01")
    assert digest["prompt_count"] == 0
    assert digest["active_minutes"] == 0
    assert digest["sessions"] == []
    assert digest["first_activity"] is None


def test_daily_digest_rejects_a_bad_date(loaded, cfg):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        daily_digest(loaded, config=cfg, date="last tuesday")


def test_daily_digest_defaults_to_today(store, cfg):
    digest = daily_digest(store, config=cfg)
    assert digest["date"]


# ---------------------------------------------------------------------------
# top_terms
# ---------------------------------------------------------------------------


def test_top_terms_skips_placeholders_and_stopwords(store):
    store.insert(make_record("email [EMAIL_1] about [EMAIL_1] and about parser parser parser"))
    terms = [t["term"] for t in top_terms(store.list())]
    assert "parser" in terms
    assert "about" not in terms
    assert not any(t.startswith("[") or t.upper() == "EMAIL_1" for t in terms)
    assert "email_1" not in terms


def test_top_terms_limit(store):
    store.insert(make_record(" ".join(f"word{i:02d}" for i in range(40))))
    assert len(top_terms(store.list(), limit=5)) == 5


# ---------------------------------------------------------------------------
# config wiring
# ---------------------------------------------------------------------------


def test_idle_gap_from_config_changes_the_split(loaded, apc_home):
    wide = Config(home=apc_home, idle_gap_minutes=180.0, tail_minutes=5.0)
    summary = time_summary(loaded, config=wide, since=at("00:00"), group_by="source")
    assert summary["total_active_minutes"] == 145.0  # one session: 09:00 -> 11:20 + 5


def test_other_sources_are_included(store, cfg):
    store.insert(make_record("a", ts=at("09:00"), source=Source.OPENCODE, project="x"))
    store.insert(make_record("b", ts=at("09:10"), source=Source.CHATGPT_WEB, project="y"))
    summary = time_summary(store, config=cfg, since=at("00:00"), group_by="source")
    assert {g["key"] for g in summary["groups"]} == {"opencode", "chatgpt_web"}


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


def test_a_turn_end_before_its_prompt_never_yields_negative_time(store, cfg):
    """Clock skew between the Stop hook and the prompt writer must not go negative."""
    store.insert(
        make_record(
            "skewed turn",
            ts=at("10:00"),
            turn_end_ts=at("09:50"),  # earlier than ts
            project="alpha",
            session_id="skew",
        )
    )
    records = store.list(order="asc")
    assert agent_seconds(records[0]) is None

    sessions = build_activity_sessions(records, idle_gap_minutes=30.0, tail_minutes=5.0)
    assert all(session.active_minutes >= 0 for session in sessions)
    assert sessions[0].end >= sessions[0].start

    summary = time_summary(store, config=cfg, since=at("00:00"), group_by="project")
    assert summary["total_active_minutes"] >= 0
    assert all(group["active_minutes"] >= 0 for group in summary["groups"])
    assert all(group["agent_minutes"] >= 0 for group in summary["groups"])


def test_skew_does_not_split_an_activity_session(store):
    store.insert(make_record("a", ts=at("10:00"), turn_end_ts=at("09:00"), session_id="s"))
    store.insert(make_record("b", ts=at("10:05"), session_id="s"))
    sessions = build_activity_sessions(store.list(order="asc"), idle_gap_minutes=30.0)
    assert len(sessions) == 1


def test_an_open_prompt_at_the_end_of_the_range_is_still_credited(loaded, cfg):
    """The last fixture prompt has no turn end; it must not vanish or go negative."""
    summary = time_summary(store=loaded, config=cfg, since=at("11:10"), group_by="project")
    assert summary["groups"]
    assert summary["total_active_minutes"] == 5.0  # tail only: one open prompt
    assert summary["groups"][0]["prompt_count"] == 1
    assert summary["groups"][0]["agent_minutes"] == 0.0
    assert summary["groups"][0]["avg_think_seconds"] is None


def test_a_session_straddling_the_since_boundary_is_truncated_not_broken(loaded, cfg):
    """Only rows inside the window exist, so the session is clipped - never negative."""
    summary = time_summary(store=loaded, config=cfg, since=at("09:15"), group_by="project")
    assert summary["total_active_minutes"] > 0
    assert summary["context_switches"] >= 0
    keys = {group["key"] for group in summary["groups"]}
    assert "beta" in keys


def test_empty_store_returns_well_formed_zeros(store, cfg):
    summary = time_summary(store, config=cfg, since="7d", group_by="project")
    assert summary["groups"] == []
    assert summary["total_active_minutes"] == 0
    assert summary["context_switches"] == 0

    timeline = activity_timeline(store, config=cfg, since="24h", bucket="hour")
    assert all(bucket["prompt_count"] == 0 for bucket in timeline["buckets"])
    assert all(bucket["active_minutes"] == 0.0 for bucket in timeline["buckets"])

    digest = daily_digest(store, config=cfg, date=DAY)
    assert digest["active_minutes"] == 0
    assert digest["prompt_count"] == 0
    assert digest["sessions"] == []
    assert digest["top_terms"] == []
    assert digest["first_activity"] is None
    assert digest["last_activity"] is None
    assert digest["context_switches"] == 0


def test_activity_timeline_buckets_are_contiguous_and_cover_every_prompt(loaded, cfg):
    timeline = activity_timeline(
        loaded, config=cfg, since=at("08:00"), until=at("13:00"), bucket="hour"
    )
    buckets = timeline["buckets"]
    assert sum(bucket["prompt_count"] for bucket in buckets) == len(FIXTURE)
    starts = [bucket["start"] for bucket in buckets]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_activity_timeline_is_not_quadratic(store, cfg):
    """50k rows x a year of daily buckets must not be rows x buckets work."""
    import time

    base = 1_758_000_000  # epoch seconds, arbitrary
    rows = []
    for index in range(50_000):
        moment = datetime.fromtimestamp(base + index * 600, tz=UTC)
        rows.append(
            (
                f"id-{index}",
                to_iso(moment),
                "claude_code",
                f"prompt number {index}",
                "hash",
                "s1",
                None,
                None,
                "proj",
                10,
                "{}",
                "{}",
                to_iso(moment + timedelta(seconds=60)),
            )
        )
    store._conn.executemany(
        "INSERT INTO prompts (id, ts, source, prompt, prompt_hash, session_id, account, cwd,"
        " project, char_count, pii_findings, metadata, turn_end_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    store._conn.commit()

    started = time.monotonic()
    timeline = activity_timeline(
        store,
        config=cfg,
        since=to_iso(datetime.fromtimestamp(base, tz=UTC)),
        until=to_iso(datetime.fromtimestamp(base + 50_000 * 600, tz=UTC)),
        bucket="day",
    )
    elapsed = time.monotonic() - started
    assert sum(b["prompt_count"] for b in timeline["buckets"]) == 50_000
    assert elapsed < 20.0, f"activity_timeline took {elapsed:.1f}s on 50k rows"

    started = time.monotonic()
    summary = time_summary(store, config=cfg, since="10y", group_by="day")
    assert time.monotonic() - started < 20.0
    assert summary["total_active_minutes"] > 0


def test_activity_timeline_truncates_instead_of_looping_forever(store, cfg):
    from agent_prompt_capture.timeline import MAX_TIMELINE_BUCKETS

    timeline = activity_timeline(store, config=cfg, since="10y", bucket="hour")
    assert len(timeline["buckets"]) <= MAX_TIMELINE_BUCKETS
    assert timeline.get("truncated") is True


# -- local time / DST -------------------------------------------------------


@pytest.fixture
def new_york(monkeypatch):
    """A zone with DST, so a fixed UTC offset is provably the wrong answer."""
    import time as time_mod

    monkeypatch.setenv("TZ", "America/New_York")
    time_mod.tzset()
    yield
    monkeypatch.undo()
    time_mod.tzset()


def test_local_helpers_follow_dst(new_york):
    from agent_prompt_capture.timeline import from_local, to_local

    # EST (UTC-5) in January, EDT (UTC-4) in July.
    assert to_local(datetime(2026, 1, 15, 12, tzinfo=UTC)).hour == 7
    assert to_local(datetime(2026, 7, 15, 12, tzinfo=UTC)).hour == 8
    assert from_local(datetime(2026, 1, 15)) == datetime(2026, 1, 15, 5, tzinfo=UTC)
    assert from_local(datetime(2026, 7, 15)) == datetime(2026, 7, 15, 4, tzinfo=UTC)
    # The autumn transition makes a 25-hour local day.
    day = from_local(datetime(2026, 11, 2)) - from_local(datetime(2026, 11, 1))
    assert day == timedelta(hours=25)


def test_hour_of_day_uses_the_offset_in_force_at_that_instant(store, cfg, new_york):
    """A fixed 'now' offset mislabels timestamps from the other side of a DST change."""
    store.insert(make_record("winter work", ts="2026-01-15T12:00:00.000Z", session_id="w"))
    store.insert(make_record("summer work", ts="2026-07-15T12:00:00.000Z", session_id="s"))
    summary = time_summary(store, config=cfg, since="10y", group_by="hour_of_day")
    keys = {group["key"] for group in summary["groups"] if group["prompt_count"]}
    assert keys == {"07", "08"}


def test_daily_digest_uses_local_midnight_boundaries(store, cfg, new_york):
    """23:30 local on the 15th is 03:30 UTC on the 16th; it belongs to the 15th."""
    store.insert(make_record("late night", ts="2026-07-16T03:30:00.000Z", session_id="n"))
    store.insert(make_record("next morning", ts="2026-07-16T13:00:00.000Z", session_id="m"))
    fifteenth = daily_digest(store, config=cfg, date="2026-07-15")
    sixteenth = daily_digest(store, config=cfg, date="2026-07-16")
    assert fifteenth["prompt_count"] == 1
    assert sixteenth["prompt_count"] == 1


def test_daily_digest_does_not_double_count_the_midnight_boundary(store, cfg, new_york):
    """``until`` is inclusive in the store, so exact local midnight must land once."""
    store.insert(make_record("on the stroke", ts="2026-07-16T04:00:00.000Z", session_id="x"))
    assert daily_digest(store, config=cfg, date="2026-07-15")["prompt_count"] == 0
    assert daily_digest(store, config=cfg, date="2026-07-16")["prompt_count"] == 1


# ---------------------------------------------------------------------------
# review regressions (cubic)
# ---------------------------------------------------------------------------


@pytest.fixture
def utc_zone(monkeypatch):
    """Pin the local zone to UTC so a day boundary is a fixed instant."""
    import time as time_mod

    monkeypatch.setenv("TZ", "UTC")
    time_mod.tzset()
    yield
    monkeypatch.undo()
    time_mod.tzset()


def test_daily_digest_clips_a_turn_that_crosses_local_midnight(store, cfg, utc_zone):
    """A turn running past midnight must not be credited to (or reported on) this day."""
    store.insert(
        make_record(
            "kicked off a long build",
            ts="2026-09-19T23:50:00.000Z",
            turn_end_ts="2026-09-20T00:30:00.000Z",
            project="alpha",
            session_id="late",
        )
    )
    digest = daily_digest(store, config=cfg, date=DAY)
    assert digest["last_activity"] == "2026-09-20T00:00:00.000Z"
    assert digest["active_minutes"] == 10.0  # 23:50 -> midnight, not 23:50 -> 00:35
    assert digest["sessions"][0]["end"] == "2026-09-20T00:00:00.000Z"
    assert digest["sessions"][0]["active_minutes"] == 10.0


def test_daily_digest_clips_the_tail_at_midnight(store, cfg, utc_zone):
    """The 5-minute tail must not spill into tomorrow either."""
    store.insert(
        make_record(
            "last thing tonight",
            ts="2026-09-19T23:58:00.000Z",
            turn_end_ts="2026-09-19T23:59:00.000Z",
            project="alpha",
            session_id="tail",
        )
    )
    digest = daily_digest(store, config=cfg, date=DAY)
    assert digest["active_minutes"] == 2.0  # 23:58 -> midnight, not 1 + 5 tail
    assert digest["last_activity"] == at("23:59")


def test_activity_timeline_keeps_a_prompt_landing_exactly_on_until(store, cfg):
    """`until` is inclusive in the store, so its bucket must exist."""
    store.insert(make_record("on the edge", ts=at("12:00"), project="alpha"))
    result = activity_timeline(
        store, config=cfg, since=at("10:00"), until=at("12:00"), bucket="hour"
    )
    assert sum(b["prompt_count"] for b in result["buckets"]) == 1


def test_source_totals_are_not_split_by_project(loaded, cfg):
    """Grouping a source's stream per project loses the time between two projects."""
    summary = time_summary(loaded, config=cfg, since=at("00:00"), group_by="source")
    assert [g["key"] for g in summary["groups"]] == ["claude_code"]
    assert summary["groups"][0]["active_minutes"] == EXPECTED_TOTAL_ACTIVE


def test_two_sources_sharing_a_session_id_stay_apart(store, cfg):
    """Session ids are only unique per agent; the group key carries the source."""
    store.insert(
        make_record("claude side", ts=at("09:00"), source=Source.CLAUDE_CODE, session_id="1")
    )
    store.insert(
        make_record("opencode side", ts=at("09:05"), source=Source.OPENCODE, session_id="1")
    )
    summary = time_summary(store, config=cfg, since=at("00:00"), group_by="session")
    assert {g["key"]: g["prompt_count"] for g in summary["groups"]} == {
        "claude_code:1": 1,
        "opencode:1": 1,
    }
