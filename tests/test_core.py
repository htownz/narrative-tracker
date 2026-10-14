"""Fast, network-free tests for the pieces that have bitten us: dedup rule, window/timezone math,
tweet flattening, pull resume/refresh planning, overrides, and the chart's data contract.

    pip install pytest && pytest -q
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from tracker import dedup, store as store_mod
from tracker.config import Incident, _resolve_window, load_incident, save_lanes
from tracker.pull import build_day_queries, day_bounds_utc
from tracker.store import Store, flatten_tweet, parse_created_at

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ dedup
def test_containment_exact_and_one_word_edit():
    a = Counter(dedup.normalise("TEXAS SENATE RACE: Where is James Talarico and does anyone care"))
    b = Counter(dedup.normalise("Texas senate race: where is James Talarico and does anybody care"))
    assert dedup.containment(a, a) == 1.0
    assert 0.85 <= dedup.containment(a, b) < 1.0   # one word differs out of eleven


def test_short_post_inside_long_post_is_not_a_copy():
    short = Counter(dedup.normalise("Where is James Talarico?"))
    long_ = Counter(dedup.normalise("Where is James Talarico? A private jet based in Austin flew to a small airport near a mental health center, and nobody from the campaign will say why"))
    assert dedup.containment(short, long_) == 0.0


def test_find_copies_marks_origin_as_earliest():
    posts = [
        {"id": "3", "text": "Where is James Talarico, and does anyone care?", "created_at": "2026-10-08T03:00:00+00:00"},
        {"id": "1", "text": "Where is James Talarico and does anyone care", "created_at": "2026-10-08T01:00:00+00:00"},
        {"id": "2", "text": "where is james talarico and does anyone CARE https://t.co/x", "created_at": "2026-10-08T02:00:00+00:00"},
        {"id": "9", "text": "Completely unrelated post about the Astros bullpen tonight", "created_at": "2026-10-08T02:30:00+00:00"},
    ]
    origins = dedup.find_copies(posts, k=3, threshold=0.9)
    assert origins["1"] is None and origins["2"] == "1" and origins["3"] == "1" and origins["9"] is None


def test_normalise_strips_urls_mentions_and_case():
    assert dedup.normalise("@GregAbbott_TX Where IS he? https://t.co/abc") == ["where", "is", "he"]


# ------------------------------------------------------------------ windows and time
def test_day_bounds_cover_dst_fallback():
    # Nov 1, 2026 is the US fall-back day: 25 local hours, bounds still land on local midnight
    since, until = day_bounds_utc(date(2026, 11, 1), "America/Chicago")
    assert since == "2026-11-01_05:00:00_UTC" and until == "2026-11-02_06:00:00_UTC"


def test_resolve_window_fixed_live_and_rolling():
    s, e, live = _resolve_window({"start": "2026-10-06", "end": "2026-10-13"}, "America/Chicago")
    assert (s, e, live) == ("2026-10-06", "2026-10-13", False)
    s, e, live = _resolve_window({"start": "2026-10-06", "end": "today"}, "America/Chicago")
    assert live and s == "2026-10-06" and e >= "2026-10-13"
    s, e, live = _resolve_window({"end": "2026-10-13", "rolling_days": 3}, "UTC")
    assert (s, e) == ("2026-10-11", "2026-10-13")


def test_parse_created_at_twitter_and_iso():
    assert parse_created_at("Mon Oct 12 23:32:30 +0000 2026") == "2026-10-12T23:32:30+00:00"
    assert parse_created_at("2026-10-12T18:32:30-05:00") == "2026-10-12T23:32:30+00:00"
    assert parse_created_at("") is None


def test_build_day_queries_one_per_query_per_day(tmp_path):
    inc = _incident(tmp_path, start="2026-10-06", end="2026-10-07", queries=['"Greg Abbott"', "@GregAbbott_TX"])
    dq = build_day_queries(inc)
    assert len(dq) == 4 and dq[0][0] == date(2026, 10, 6)
    assert "since:2026-10-06_05:00:00_UTC" in dq[0][1] and "min_faves:10" in dq[0][1] and "-filter:retweets" in dq[0][1]


# ------------------------------------------------------------------ flattening and store
def test_flatten_tweet_tolerates_missing_fields():
    row = flatten_tweet({"id": "42", "text": "hi", "createdAt": "Mon Oct 12 23:32:30 +0000 2026"})
    assert row["id"] == "42" and row["view_count"] == 0 and row["author_handle"] == "" and row["quoted_id"] is None
    assert row["url"] == ""


def test_store_upsert_keeps_labels_and_dataset_flag(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_posts([flatten_tweet({"id": "1", "text": "a", "createdAt": "Mon Oct 12 23:32:30 +0000 2026", "viewCount": 5000}, in_dataset=1)])
    st.set_lanes({"1": "pushback"}, "claude")
    # a later refresh re-upserts the same post as a reference row with fresher views
    st.upsert_posts([flatten_tweet({"id": "1", "text": "a", "createdAt": "Mon Oct 12 23:32:30 +0000 2026", "viewCount": 9000}, in_dataset=0)])
    row = st.conn.execute("select in_dataset, lane, view_count from posts where id='1'").fetchone()
    assert tuple(row) == (1, "pushback", 9000)
    st.close()


# ------------------------------------------------------------------ incidents and lanes
def test_save_lanes_keeps_comments(tmp_path):
    inc = _incident(tmp_path, lanes_literal="lanes: []", header="# keep me\n")
    save_lanes(inc, [{"key": "a", "label": "A", "description": "d", "keywords": ["x"]}])
    text = inc.path.read_text(encoding="utf-8")
    assert text.startswith("# keep me") and "lanes:\n- key: a" in text
    assert load_incident(inc.path).lanes[0]["label"] == "A"


def test_lane_table_appends_pushback_with_stable_colors(tmp_path):
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}\n  - {key: y, label: Y}")
    t = inc.lane_table()
    assert [l["key"] for l in t] == ["x", "y", "pushback"] and t[0]["color"] != t[1]["color"]


def test_overrides_apply_and_ignore_unknown_lanes(tmp_path, monkeypatch):
    from tracker import review
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    st.upsert_posts([flatten_tweet({"id": "1", "text": "a", "createdAt": "Mon Oct 12 23:32:30 +0000 2026", "viewCount": 5000})])
    st.set_lanes({"1": "x"}, "claude"); st.close()
    (ROOT / "incidents" / f"{inc.slug}.overrides.csv").write_text("post_id,lane\n1,pushback\n1b,nope\n", encoding="utf-8")
    try:
        n = review.apply_overrides(inc)
        st = Store(inc.db_path)
        assert n == 1 and st.conn.execute("select lane, lane_source from posts where id='1'").fetchone()[0] == "pushback"
        st.close()
    finally:
        (ROOT / "incidents" / f"{inc.slug}.overrides.csv").unlink()


# ------------------------------------------------------------------ chart contract
def test_chart_template_has_data_marker_and_escapes():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "/*__DATA__*/" in html and "const esc =" in html and "window.__tracker" in html


# ------------------------------------------------------------------ helpers
def _incident(tmp_path, start="2026-10-06", end="2026-10-13", queries=("x",), lanes_literal="lanes: []", header="") -> Incident:
    from tracker import config
    config.DATA_DIR = tmp_path
    y = tmp_path / "t-test.yaml"
    q = "\n".join(f"  - '{x}'" for x in queries)
    y.write_text(f"""{header}slug: t-test
title: "T {{days}}"
subject: s
window: {{start: "{start}", end: "{end}"}}
timezone: America/Chicago
queries:
{q}
min_faves_hint: 10
exclude_retweets: true
min_views: 1000
{lanes_literal}
pushback: {{label: Pushback, description: d}}
""", encoding="utf-8")
    inc = load_incident(y)
    return inc
