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
    assert keys == {"s1": 3, "s2": 2}


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
