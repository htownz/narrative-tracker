"""Fast, network-free tests for the pieces that have bitten us: dedup rule, window/timezone math,
tweet flattening, pull resume/refresh planning, overrides, and the chart's data contract.

    pip install pytest && pytest -q
"""
from __future__ import annotations

import csv
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
    config.OUT_DIR = tmp_path / "out"
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


# ------------------------------------------------------------------ claims
class _FakeClient:
    """Stands in for the Anthropic client: extraction returns one claim per post, clustering groups by first word."""
    class messages:  # noqa: N801
        @staticmethod
        def create(model, max_tokens, system, messages):
            import types
            user = messages[0]["content"]
            if "Posts (id | handle | text):" in user:
                ids = [l.split(" | ")[0] for l in user.split("Posts (id | handle | text):\n")[1].split("\n\nReturn JSON")[0].splitlines()]
                claims = {pid: [{"text": f"Paxton skipped debate {pid}", "type": "fact", "checkable": True, "stance": "against", "entities": ["Ken Paxton"]}]
                          for pid in ids}
                claims[ids[-1]] = []  # last post makes no claim
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps({"claims": claims}))])
            new = [l.split(" | ")[0] for l in user.split("New claims (id | text):\n")[1].split("\n\nFor each")[0].splitlines()]
            out = {"assign": {nid: "g1" for nid in new}, "groups": {"g1": {"text": "Ken Paxton has refused every debate.", "type": "fact", "checkable": True, "stance": "against", "entities": ["Ken Paxton"]}}}
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(out))])


def test_claims_lexical_helpers_and_validation():
    from tracker import claims
    a, b = claims.tokens("Ken Paxton refused every debate invitation."), claims.tokens("Paxton has refused every debate")
    assert claims.jaccard(a, b) >= 0.6 and claims.jaccard(a, set()) == 0.0
    assert claims._valid_claim({"text": "  Paxton   lied ", "type": "FACT", "checkable": "yes", "stance": "Against", "entities": "x"}) == \
        {"text": "Paxton lied", "type": "fact", "checkable": True, "stance": "against", "entities": []}
    assert claims._valid_claim({"text": "ok"}) is None and claims._valid_claim("nope") is None


def test_claims_extract_cluster_summarise_incrementally(tmp_path, monkeypatch):
    from tracker import claims
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    rows = [flatten_tweet({"id": str(i), "text": f"post {i}", "createdAt": f"Mon Oct 12 {10 + i:02d}:00:00 +0000 2026", "viewCount": 5000 * (i + 1),
                           "author": {"userName": f"u{i % 2}"}}) for i in range(4)]
    st.upsert_posts(rows)
    st.set_lanes({"0": "x", "1": "x", "2": "pushback", "3": "other"}, "claude")
    assert claims.extract(inc, st, _FakeClient()) == 3                      # 'other' is skipped
    assert st.conn.execute("select count(*) from claims").fetchone()[0] == 2   # the last post made no claim
    assert claims.extract(inc, st, _FakeClient()) == 0                      # nothing pending the second time
    stats = claims.cluster(inc, st, _FakeClient())
    assert stats["new_clusters"] == 1 and stats["model"] == 2
    # a later claim with nearly the same words joins lexically, without the model
    st.add_claims("3", [{"text": "Ken Paxton has refused every debate", "type": "fact", "checkable": True, "stance": "against"}], "2026-10-13T00:00:00+00:00")
    stats = claims.cluster(inc, st, None)
    assert stats["lexical"] == 1 and stats["model"] == 0
    data = claims.summarise(inc, st)
    c = data["clusters"][0]
    # extraction went biggest-first, so post 0 (smallest) was the one that made no claim; posts 1, 2, 3 remain
    assert data["stats"]["clusters"] == 1 and c["posts"] == 3 and c["accounts"] == 2 and c["first_handle"] == "u1"
    assert c["views"] == 10000 + 15000 + 20000 and c["pushback_posts"] == 1 and [l["key"] for l in c["lanes"]][0] == "x"
    page = claims.render_html(inc, data)
    text = page.read_text(encoding="utf-8")
    assert "Ken Paxton has refused every debate." in text and "checkable" in text
    st.close()


# ------------------------------------------------------------------ control score + sub-threshold copies
def _post(i, handle, views, created, origin=None, account_created="Mon Jan 01 00:00:00 +0000 2018", lane="x"):
    raw = json.dumps({"author": {"createdAt": account_created}})
    return {"id": str(i), "author_handle": handle, "view_count": views, "created_at": created, "copy_origin": origin, "raw": raw, "lane": lane, "in_dataset": 1}


def test_control_flags_a_clockwork_ring_and_not_organic_spread():
    from tracker import control
    # ring: one origin, nine copies from nine new accounts, every ten minutes inside one hour
    ring = [_post(0, "origin", 50_000, "2026-10-10T12:00:00+00:00")]
    ring += [_post(i, f"new{i}", 800, f"2026-10-10T12:{i*6:02d}:00+00:00", origin="0", account_created="Tue Sep 01 00:00:00 +0000 2026") for i in range(1, 10)]
    s_ring, why = control.score(control.features(ring, {"0": "origin"}))
    assert s_ring >= control.FLAG and any("copies" in w for w in why) and any("days old" in w for w in why)
    # organic: twelve accounts over two days, no copies, old accounts
    organic = [_post(i, f"acc{i}", 3_000 + i * 500, f"2026-10-{10 + i // 6:02d}T{8 + (i % 6) * 2:02d}:00:00+00:00") for i in range(12)]
    s_org, _ = control.score(control.features(organic, {}))
    assert s_org < 30
    # an account repeating itself is not a ring
    selfspam = [_post(0, "me", 5000, "2026-10-10T12:00:00+00:00")] + [_post(i, "me", 900, f"2026-10-10T12:{i*5:02d}:00+00:00", origin="0") for i in range(1, 8)]
    f = control.features(selfspam, {"0": "me"})
    assert f["copies_n"] == 0


def test_dedup_draws_small_copies_with_the_rings_lane(tmp_path):
    from tracker import dedup as dedup_mod
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    text = "Ken Paxton has asked for proof of life from me; I'm asking for proof of spine from him."
    rows = [flatten_tweet({"id": "1", "text": text, "createdAt": "Mon Oct 12 10:00:00 +0000 2026", "viewCount": 40000, "author": {"userName": "big"}}),
            flatten_tweet({"id": "2", "text": text, "createdAt": "Mon Oct 12 11:00:00 +0000 2026", "viewCount": 120, "author": {"userName": "small"}}),
            flatten_tweet({"id": "3", "text": "totally different words about the weather in Austin today", "createdAt": "Mon Oct 12 11:00:00 +0000 2026", "viewCount": 90, "author": {"userName": "nobody"}}),
            flatten_tweet({"id": "4", "text": "totally different words about the weather in Austin today", "createdAt": "Mon Oct 12 12:00:00 +0000 2026", "viewCount": 80, "author": {"userName": "nobody2"}})]
    st.upsert_posts(rows); st.set_lanes({"1": "x"}, "claude"); st.close()
    dedup_mod.dedup(inc)
    st = Store(inc.db_path)
    drawn = {p["id"]: p for p in st.drawable(inc.min_views)}
    assert set(drawn) == {"1", "2"}                       # the small copy of a drawn post is drawn; the small-only ring is not
    assert drawn["2"]["lane"] == "x" and drawn["2"]["lane_source"] == "copy" and drawn["2"]["copy_origin"] == "1"
    assert st.conn.execute("select copy_origin from posts where id='4'").fetchone()[0] is None
    st.close()


def test_overrides_become_few_shot_examples(tmp_path):
    from tracker import lanes
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    st.upsert_posts([flatten_tweet({"id": str(i), "text": f"example post {i}", "createdAt": "Mon Oct 12 23:32:30 +0000 2026", "viewCount": 5000, "author": {"userName": "h"}}) for i in range(25)])
    (ROOT / "incidents" / f"{inc.slug}.overrides.csv").write_text("post_id,lane\n" + "".join(f"{i},{'x' if i % 2 else 'pushback'}\n" for i in range(25)) + "99,nope\n", encoding="utf-8")
    try:
        ex = lanes.few_shot(inc, st, {"x", "pushback", "other"})
        assert ex.count("\n@h | ") == lanes.EXAMPLES_MAX and ex.splitlines()[2].startswith("@h | example post 24 -> ")   # newest first, capped
        assert "nope" not in ex and lanes.few_shot(inc, None, {"x"}) == ""
        assert "{examples}" in lanes.CLASSIFY_USER and lanes.CLASSIFY_USER.format(subject="s", lanes="", pushback="", examples="", posts="")
    finally:
        (ROOT / "incidents" / f"{inc.slug}.overrides.csv").unlink()
        st.close()


# ------------------------------------------------------------------ events
def test_events_load_window_response_and_follow(tmp_path):
    from tracker import events
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    ev_path = tmp_path / "events.yaml"
    ev_path.write_text("""timezone: America/Chicago
events:
  - {at: "2026-10-08 09:00", label: Poll, type: poll, trackers: [t-test]}
  - {at: "2026-10-09", label: Rally, type: rally, trackers: all}
  - {at: "2026-10-10 09:00", label: Elsewhere, type: news, trackers: [other-slug]}
  - {at: "bad", label: Broken, type: news, trackers: all}
""", encoding="utf-8")
    ev = events.load_events(inc, ev_path)
    assert [e["label"] for e in ev] == ["Poll", "Rally"]
    assert ev[0]["at"] == "2026-10-08T14:00:00+00:00" and not ev[0]["all_day"]      # 9 AM CDT -> 14:00 UTC
    assert ev[1]["at"] == "2026-10-09T05:00:00+00:00" and ev[1]["all_day"]
    lanes = [{"key": "x", "label": "X", "color": "#fff"}]
    posts = [{"id": "a", "t": "2026-10-08T10:00:00+00:00", "lane": "x", "views": 1000},    # 4h before the poll
             {"id": "b", "t": "2026-10-08T15:00:00+00:00", "lane": "x", "views": 5000},    # 1h after
             {"id": "c", "t": "2026-10-09T12:00:00+00:00", "lane": "x", "views": 700}]     # rally day
    from datetime import datetime
    rows = events.response(posts, lanes, ev, data_end=datetime.fromisoformat("2026-10-10T12:00:00+00:00"))
    assert rows[0]["lanes"]["x"]["ratio"] == 5.0 and rows[0]["after_views"] == 5000 and rows[0]["top_lane"] == "x"
    assert rows[1]["lanes"]["x"]["before_views"] == 6000 and rows[1]["lanes"]["x"]["after_views"] == 700   # day before vs the day
    assert events.response(posts, lanes, ev, data_end=datetime.fromisoformat("2026-10-09T03:00:00+00:00")) == rows[:1]   # not enough data after the rally yet
    assert events.nearest_before("2026-10-08T19:00:00+00:00", ev)["label"] == "Poll"
    assert events.nearest_before("2026-10-08T21:00:00+00:00", ev) is None            # 7h later: outside the follow window
    assert events.nearest_before("2026-10-09T23:00:00+00:00", ev)["label"] == "Rally"  # anywhere inside an all-day event


# ------------------------------------------------------------------ prediction features
def test_features_rank_and_outcomes():
    from tracker import features
    from datetime import datetime
    base = {"kind": "claim", "accounts": 4, "views_6h": 10_000, "momentum_6h": 2.0, "control": 10, "event_follow": 0}
    ranked = features.rank_now([{**base, "key": "a"}, {**base, "key": "b", "views_6h": 50_000}, {**base, "key": "c", "accounts": 1},
                                {**base, "key": "d", "control": 60}, {**base, "key": "lane", "kind": "lane"}])
    assert {r["key"] for r in ranked[:2]} == {"b", "d"} and all(r["key"] not in ("c", "lane") for r in ranked)
    t0 = "2026-10-10T12:00:00+00:00"
    rows = [{"asof": t0, "kind": "claim", "key": k, "gain_24h": None, "gain_72h": None, "top5_24h": None} for k in "abcdefg"]
    groups = {"claim": {k: [{"created_at": f"2026-10-10T{13 + i:02d}:00:00+00:00", "view_count": (ord(k) - 96) * 1000}] for i, k in enumerate("abcdefg")}}
    n = features._label_outcomes(rows, groups, datetime.fromisoformat("2026-10-11T13:00:00+00:00"))   # 25h later: 24h outcome only
    assert n == 7 and rows[6]["gain_24h"] == 7000 and rows[6]["top5_24h"] == 1 and rows[0]["top5_24h"] == 0 and rows[0]["gain_72h"] is None
    assert features._label_outcomes(rows, groups, datetime.fromisoformat("2026-10-11T14:00:00+00:00")) == 0       # already filled


# ------------------------------------------------------------------ site navigation
def test_site_nav_keeps_tab_and_marks_current():
    from tracker import site
    trackers = [{"slug": "a-live", "title": "A", "live": True, "group": "Races and people", "pages": ["index.html", "claims.html"]},
                {"slug": "issue-b-live", "title": "B", "live": True, "group": "Issues", "pages": ["index.html"]}]
    nav = site.nav_html(trackers, "a-live", "claims.html")
    assert 'value="../a-live/claims.html" selected' in nav and 'value="../issue-b-live/"' in nav      # B has no claims page: falls back to its chart
    assert '<a href="../a-live/claims.html" class="on">Claims</a>' in nav and 'href="../accounts/"' in nav
    hub_nav = site.nav_html(trackers, None, None, depth=0)
    assert 'href="a-live/claims.html"' not in hub_nav and 'value="a-live/"' in hub_nav
    assert site.inject("<html><body class='x'><p>hi</p></body></html>", "<nav>n</nav>").startswith("<html><body class='x'>\n<nav>n</nav>")


def test_slim_raw_keeps_what_analysis_reads_and_compact_is_idempotent(tmp_path):
    from tracker.store import slim_raw
    t = {"id": "1", "createdAt": "Mon Oct 12 23:32:30 +0000 2026", "viewCount": 5, "text": "hi", "card": {"big": "x" * 5000},
         "extendedEntities": {"media": [{"type": "photo", "url": "u"}]}, "entities": {"urls": [{"expanded_url": "https://a.b/c"}], "hashtags": [{"text": "txlege"}]},
         "quoted_tweet": {"id": 99, "author": {"userName": "q"}, "text": "y" * 3000},
         "author": {"userName": "h", "createdAt": "Tue Sep 01 00:00:00 +0000 2026", "followers": 10, "profile_bio": {"x": "y" * 2000}, "description": "d" * 500}}
    s = slim_raw(t)
    assert s["author"]["createdAt"] == t["author"]["createdAt"] and s["quoted_tweet"] == {"id": "99", "author": "q"} and s["media"] == ["photo"]
    assert s["entities"] == {"urls": ["https://a.b/c"], "hashtags": ["txlege"]} and len(s["author"]["description"]) == 200 and "card" not in s
    assert slim_raw(s) == s and len(json.dumps(s)) < 600
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    st.upsert_posts([flatten_tweet(t)])
    st.conn.execute("UPDATE posts SET raw=? WHERE id='1'", (json.dumps(t),)); st.conn.commit()   # a pre-slim row
    assert st.compact() == 1 and st.compact() == 0
    from tracker import control
    assert control.account_age_days(dict(st.conn.execute("select * from posts where id='1'").fetchone())) > 40
    st.close()


# ------------------------------------------------------------------ bucket
def test_bucket_push_pull_snapshot_round_trip(tmp_path, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3, sqlite3 as sq
    for k, v in {"BUCKET_NAME": "nt-test", "BUCKET_ACCESS_KEY": "testing", "BUCKET_SECRET_KEY": "testing", "BUCKET_REGION": "us-east-1", "BUCKET_ENDPOINT": ""}.items():
        monkeypatch.setenv(k, v)
    from tracker import bucket
    monkeypatch.setattr(bucket, "DATA_DIR", tmp_path)
    (tmp_path / "features").mkdir()
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="nt-test")
        for n in ("a-live", "b-live"):
            c = sq.connect(tmp_path / f"{n}.sqlite"); c.execute("create table t(x)"); c.execute("insert into t values (1)"); c.commit(); c.close()
        (tmp_path / "features" / "a-live.csv").write_text("run_at,x\n1,2\n")
        assert bucket.pull() == 0 and bucket.push() == 3 and bucket.push() == 0        # empty bucket; seed; unchanged
        c = sq.connect(tmp_path / "a-live.sqlite"); c.execute("insert into t values (2)"); c.commit(); c.close()
        assert bucket.push() == 1                                                     # only the changed store
        (tmp_path / "b-live.sqlite").unlink()
        assert bucket.pull() == 1 and (tmp_path / "b-live.sqlite").exists()           # a missing store comes back
        assert bucket.snapshot(keep=2) == 2
        keys = {o["Key"] for o in boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="nt-test")["Contents"]}
        assert any(k.startswith("snapshots/") and k.endswith("a-live.sqlite") for k in keys) and "features/a-live.csv" in keys


def test_bucket_is_a_noop_without_config(monkeypatch):
    from tracker import bucket
    for k in ("BUCKET_NAME", "BUCKET_ACCESS_KEY", "BUCKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bucket, "env", lambda *a, **k: "")
    assert not bucket.configured() and bucket.pull() == 0 and bucket.push() == 0 and bucket.snapshot() == 0


# ------------------------------------------------------------------ link rings
def test_link_rings_normalize_and_detect():
    from tracker import rings
    assert rings.normalize("https://www.nytimes.com/2026/09/30/us/politics/x.html?utm_source=tw&smid=tw-share#top") == "nytimes.com/2026/09/30/us/politics/x.html"
    assert rings.normalize("https://twitter.com/foo/status/123?s=20") == "x.com/status/123" and rings.normalize("https://t.co/abc") is None
    assert rings.normalize("https://www.youtube.com/watch?v=abc&feature=share") == "youtube.com/watch?v=abc"
    def post(i, handle, minute, url, followers=100, created="Mon Jan 01 00:00:00 +0000 2018"):
        return {"id": str(i), "author_handle": handle, "author_followers": followers, "view_count": 1000, "lane": "x", "in_dataset": 1,
                "created_at": f"2026-10-10T12:{minute:02d}:00+00:00", "text": "look", "url": "u",
                "raw": json.dumps({"entities": {"urls": [url]}, "author": {"createdAt": created}})}
    link = "https://example.com/story"
    burst = [post(i, f"small{i}", i * 5, link) for i in range(6)]                                     # six small accounts in 30 minutes
    slow = [post(10 + i, f"reporter{i}", 0, "https://news.com/a", followers=50_000) for i in range(2)] + \
           [{**post(20 + i, f"reporter{2 + i}", 0, "https://news.com/a", followers=50_000), "created_at": f"2026-10-1{1 + i}T12:00:00+00:00"} for i in range(4)]  # six accounts over four days
    found = rings.find_rings(burst + slow)
    assert len(found) == 1 and found[0]["url"] == "example.com/story" and found[0]["burst_accounts"] == 6 and found[0]["suspicious"]
    assert found[0]["small_accounts"] == 1.0 and found[0]["lanes"] == {"x": 6}


# ------------------------------------------------------------------ view history
def test_view_history_records_sightings_and_seeds_once(tmp_path):
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    t = {"id": "1", "text": "a", "createdAt": "Mon Oct 12 10:00:00 +0000 2026", "viewCount": 1500, "author": {"userName": "h"}}
    st.upsert_posts([flatten_tweet(t), flatten_tweet({**t, "id": "2", "viewCount": 50})])      # post 2 is under the threshold
    st.upsert_posts([flatten_tweet(t)])                                                          # same count again: no new row
    st.upsert_posts([flatten_tweet({**t, "viewCount": 4000})])                                   # grew: new row
    series = st.view_series(["1", "2"])
    assert [v for _, v in series["1"]] == [1500, 4000] and "2" not in series
    assert st.seed_history() == 0                                                                # history exists: nothing to seed
    st.conn.execute("DELETE FROM view_history"); st.conn.commit()
    assert st.seed_history() == 1 and st.seed_history() == 0
    st.close()


# ------------------------------------------------------------------ news (GDELT)
def test_news_query_and_summary():
    from datetime import datetime, timezone
    from tracker import news
    q = news.query_for("James Talarico was in a mental health facility, the Lindner Center, during his absence.", ["James Talarico", "Lindner Center"])
    assert q.startswith('"James Talarico" "Lindner Center" "') and q.endswith(" sourcelang:english")
    arts = [{"seendate": "20261011T200000Z", "domain": "texastribune.org"}, {"seendate": "20261012T010000Z", "domain": "cnn.com"},
            {"seendate": "20261012T020000Z", "domain": "cnn.com"}, {"seendate": "20261009T000000Z", "domain": "foxnews.com"}, {"seendate": "bad"}]
    s = news.summarise(arts, datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc))
    assert s["articles"] == 4 and s["sources"] == 3 and s["sources_48h"] == 2 and s["news_before_x"] == 1 and s["top_domains"][0] == "cnn.com"


def test_insights_counts_outcomes_only_on_competitive_days_and_reads_by_rule(tmp_path, monkeypatch):
    from tracker import insights, scorecard
    monkeypatch.setattr(insights, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(insights, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(scorecard, "DATA_DIR", tmp_path / "data")
    (tmp_path / "out" / "big-live").mkdir(parents=True)
    (tmp_path / "data" / "features").mkdir(parents=True)
    (tmp_path / "out" / "big-live" / "data.json").write_text(json.dumps({"meta": {"title": "Big"}, "lanes": [], "posts": []}))
    (tmp_path / "out" / "big-live" / "claims.json").write_text(json.dumps({"clusters": [
        {"id": i, "text": f"claim {i}", "type": "fact", "verdict": "unsupported" if i % 2 else "supported", "views": 1000 * i,
         "lanes": [{"label": "Lane A"}], "first_handles": ["big" if i % 3 == 0 else "small"]} for i in range(1, 31)]}))
    (tmp_path / "out" / "big-live" / "analysis.json").write_text(json.dumps({"events": [{"type": "poll", "ratio": 2.5, "top_lane": "a"}, {"type": "poll", "ratio": 1.5}],
                                                                             "lanes": [], "lead_lag": [], "market": "M"}))
    base = {c: "" for c in ["run_at", "asof", "slug", "kind", "key", "label", "age_h", "posts", "views", "accounts", "copies", "first_views", "first_followers",
                            "views_1h", "views_6h", "views_24h", "momentum_6h", "control", "new_accounts", "burst", "event_follow", "event_type",
                            "market_price", "market_change_24h", "gain_24h", "gain_72h", "top5_24h"]}
    rows = []
    for i in range(1, 31):                                  # a competitive day: 30 claims, big first posters win
        rows.append({**base, "asof": "d1", "slug": "big-live", "kind": "claim", "key": str(i), "first_followers": 5_000_000 if i % 3 == 0 else 100,
                     "control": 5, "gain_24h": 10, "top5_24h": 1 if i % 3 == 0 else 0})
    for i in range(1, 4):                                   # a thin day: three claims, all trivially top five -> must not count
        rows.append({**base, "asof": "d2", "slug": "big-live", "kind": "claim", "key": str(i), "first_followers": 100, "control": 5, "gain_24h": 1, "top5_24h": 1})
    import csv
    with (tmp_path / "data" / "features" / "big-live.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(base)); w.writeheader(); w.writerows(rows)
    (tmp_path / "data" / "features" / "picks.csv").write_text("run_at,asof,slug,key,label,rank,heuristic,views_6h,accounts,eligible\nr,d1,big-live,3,claim 3,1,9.0,50000,4,1\n", encoding="utf-8")
    page = insights.insights()                                                    # the pick log sits beside the feature tables and must not be read as one
    out = json.loads((tmp_path / "out" / "insights.json").read_text(encoding="utf-8"))
    assert out["scorecard"]["n_logged"] == 1 and out["scorecard"]["all"]["hits"] == 1
    assert out["first_poster"]["n"] == 30 and {t["bucket"]: t["p_top5"] for t in out["first_poster"]["table"]}["1M+"] == 1.0
    assert "million-plus" in out["first_poster"]["reading"]
    assert out["early_touch"]["table"][0]["handle"] == "big" and out["early_touch"]["table"][0]["p_top5"] == 1.0
    assert out["events"]["table"][0] == {"type": "poll", "n": 2, "median_lift": 2.0, "share_doubling": 0.5, "lane_most_often": "a"}
    assert out["verdicts"]["n"] == 30 and out["verdicts"]["table"][0]["share_false"] == 0.5 and "(Big)" in out["verdicts"]["table"][0]["lane"]
    html_text = page.read_text(encoding="utf-8")
    assert "Insights" in html_text and "Lane A (Big)" in html_text and "100%" in html_text


def test_news_skips_claims_older_than_the_feed_and_marks_old_checks_unknown(tmp_path):
    from datetime import datetime, timedelta, timezone
    from tracker import news
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    st.conn.executescript(news.SCHEMA)
    now = datetime.now(timezone.utc)
    def tweet(i, when):
        return flatten_tweet({"id": str(i), "text": f"t{i}", "createdAt": when.strftime("%a %b %d %H:%M:%S +0000 %Y"), "viewCount": 90_000, "author": {"userName": "h"}})
    st.upsert_posts([tweet(1, now - timedelta(days=20)), tweet(2, now - timedelta(days=1))])
    old = st.add_cluster("old claim", "fact", True, "for", ["A"], now.isoformat())
    new = st.add_cluster("new claim", "fact", True, "for", ["B"], now.isoformat())
    st.add_claims("1", [{"text": "old claim", "type": "fact", "checkable": True}], now.isoformat())
    st.add_claims("2", [{"text": "new claim", "type": "fact", "checkable": True}], now.isoformat())
    st.assign_clusters({1: old, 2: new})
    todo = news.candidates(st, 50_000, 10, 12)
    assert [c["id"] for c in todo] == [new]                                     # the old claim's 48h window is beyond the 7-day feed
    for cid in (old, new):                                                      # a check recorded for each, as the job did before the fix
        st.conn.execute("INSERT INTO news_checks (cluster_id, at, query, articles, sources, articles_24h, sources_24h, articles_48h, sources_48h, first_news_at, news_before_x, top_domains) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (cid, now.isoformat(timespec="minutes"), "q", 40, 9, 0, 0, 0, 0, None, 0, "[]"))
    st.conn.commit()
    checks = news.latest_checks(st)
    assert checks[old]["covered"] is False and checks[old]["sources_48h"] is None and checks[old]["articles"] == 40
    assert checks[new]["covered"] is True and checks[new]["sources_48h"] == 0
    st.close()


def test_scorecard_logs_picks_and_scores_them_against_outcomes(tmp_path, monkeypatch):
    from tracker import scorecard
    monkeypatch.setattr(scorecard, "DATA_DIR", tmp_path)
    base = {c: "" for c in ["run_at", "asof", "slug", "kind", "key", "label", "views_6h", "momentum_6h", "accounts", "control", "event_follow", "gain_24h", "top5_24h", "measured"]}
    rows = []
    for i in range(1, 13):                                  # one competitive snapshot: 12 claims, claims 1-2 win
        rows.append({**base, "run_at": "r", "asof": "s1", "slug": "a", "kind": "claim", "key": str(i), "label": f"c{i}", "views_6h": 50_000 // i, "momentum_6h": 2.0,
                     "accounts": 4, "control": 0, "event_follow": 0, "gain_24h": 100 if i <= 2 else 1, "top5_24h": 1 if i <= 2 else 0, "measured": 1})
    ranked = [{"key": str(i), "label": f"c{i}", "heuristic": 10 - i, "views_6h": 50_000 // i, "accounts": 4} for i in range(1, 6)]
    assert scorecard.log_picks("a", ranked, "s1", "r") == 5 and scorecard.log_picks("a", ranked, "s1", "r2") == 5    # re-log replaces, no duplicates
    with (tmp_path / "features" / "picks.csv").open(encoding="utf-8") as f:
        logged = list(csv.DictReader(f))
    assert len(logged) == 5 and logged[0]["eligible"] == "1" and logged[3]["eligible"] == "0" and logged[0]["run_at"] == "r2"
    s = scorecard.scorecard(rows)
    assert s["n_scored"] == 5 and s["n_logged"] == 5 and s["all"]["hits"] == 2 and s["top3"]["hit_rate"] == 0.67 and s["base_rate"] == round(2 / 12, 3)
    assert s["latest"][0]["hit"] == 1 and "too few" in s["reading"]
    (tmp_path / "features" / "picks.csv").unlink()           # no log: the snapshot is backtested with the same heuristic
    s2 = scorecard.scorecard(rows)
    assert s2["n_logged"] == 0 and s2["n_scored"] == 10 and s2["all"]["hits"] == 2
    thin = [{**r, "asof": "s2"} for r in rows[:5]]            # five claims competing: not a result
    assert scorecard.scorecard(rows + thin)["n_scored"] == 10


def test_poll_events_from_claims_merge_spellings_and_defer_to_hand_entries(tmp_path):
    from tracker import pollevents as pe
    assert pe.pollster("A YouGov/Univision poll has Talarico leading 48% to 43%.") == "YouGov/Univision"
    assert pe.pollster("In NYT/Siena polling, Greg Abbott led 51% to 44% in June.") == "NYT/Siena"
    assert pe.pollster("A University of Houston Hobby School poll of Harris County finds") == "Houston Hobby School"
    assert pe.pollster("A Democratic-sponsored poll of TX-35 shows") is None and pe.pollster("A poll shows Paxton leading by 6 points.") is None
    assert pe.pollster("The RCP Texas Senate polling average shows Talarico ahead") is None
    assert pe.tokens("Insider Advantage") & pe.tokens("InsiderAdvantage") and pe.tokens("Univision/YouGov") == pe.tokens("YouGov/Univision")
    assert pe.subject_tokens(['"Ken Paxton"', "@KenPaxtonTX", "Paxton Talarico"]) >= {"ken paxton", "paxton", "talarico"}
    mk = lambda i, at, text, views=100_000: {"id": i, "type": "fact", "checkable": True, "views": views, "text": text, "first_seen": at, "first_handle": "h"}
    claims = {
        "paxton-live": [mk(1, "2026-10-13T11:00:00+00:00", "A University of Houston poll shows James Talarico leading Ken Paxton 58% to 36%."),
                        mk(2, "2026-10-13T17:28:00+00:00", "A Quantus poll of likely voters shows Ken Paxton at 47.2% and James Talarico at 46.7%."),
                        mk(3, "2026-10-13T12:00:00+00:00", "An Insider Polling survey shows Sherrod Brown leading Jon Husted 47% to 44% in Ohio."),
                        mk(4, "2026-10-13T12:30:00+00:00", "Ken Paxton is a criminal.", 900_000)],
        "abbott-oct-2026": [mk(5, "2026-10-13T11:08:49+00:00", "A UH/Hobby School poll of Harris County has Gina Hinojosa leading Greg Abbott 52% to 44%.")],
        "governor-race-live": [mk(6, "2026-10-13T11:08:49+00:00", "A University of Houston Hobby School poll finds Gina Hinojosa at 49% and Greg Abbott at 45%."),
                               mk(7, "2026-10-16T11:00:00+00:00", "A University of Houston poll shows Greg Abbott at 48% and Gina Hinojosa at 46%.")],
    }
    subjects = {"paxton-live": {"paxton", "talarico"}, "abbott-oct-2026": {"abbott", "hinojosa"}, "governor-race-live": {"abbott", "hinojosa"}}
    ev = pe.events_from_claims(claims, subjects)
    assert [e["label"] for e in ev] == ["Houston Hobby School, UH/Hobby School polls", "Quantus poll", "Houston poll"]   # Ohio poll dropped; the 10/16 release is separate
    assert ev[0]["trackers"] == ["abbott-oct-2026", "governor-race-live", "paxton-live"] and ev[0]["at"] == "2026-10-13T11:00:00+00:00" and "3 claims" in ev[0]["source"]
    hand = [{"label": "UH Hobby School Harris County poll", "type": "poll", "at": "2026-10-13T11:12:00+00:00", "auto": False},
            {"label": "Quantus poll: Paxton 47.2-46.7", "type": "poll", "at": "2026-10-10T17:28:00+00:00", "auto": False}]
    assert pe.covered_by(ev[0], hand) and not pe.covered_by(ev[1], hand)                                                 # same pollster 3 days apart is not the same release
    # load_events merges: hand entries, then automatic ones not covered
    from tracker import events as events_mod
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    yaml_path = tmp_path / "events.yaml"
    yaml_path.write_text('timezone: America/Chicago\nevents:\n  - {at: "2026-10-13 06:12", label: "UH Hobby School Harris County poll", type: poll, trackers: all}\n')
    import tracker.pollevents
    tracker.pollevents.auto_events.cache_clear()
    tracker.pollevents.auto_events = lambda: tuple({**e, "trackers": [inc.slug]} for e in ev)
    merged = events_mod.load_events(inc, path=yaml_path)
    assert [e["auto"] for e in merged] == [False, True, True] and merged[0]["label"].startswith("UH Hobby") and merged[1]["label"] == "Quantus poll"
    assert len(events_mod.load_events(inc, path=yaml_path, with_auto=False)) == 1


def test_registry_scores_first_posters_and_early_touches(tmp_path, monkeypatch):
    from tracker import registry
    monkeypatch.setattr(registry, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(registry, "DATA_DIR", tmp_path / "data")
    (tmp_path / "out" / "t-live").mkdir(parents=True)
    (tmp_path / "out" / "accounts").mkdir(parents=True)
    clusters = [{"id": i, "first_handles": ["alpha", "beta"] if i % 2 else ["gamma"], "first_handle": "alpha" if i % 2 else "gamma",
                 "verdict": ("supported" if i in (1, 3, 5) else "misleading" if i == 7 else None)} for i in range(1, 13)]
    (tmp_path / "out" / "t-live" / "claims.json").write_text(json.dumps({"clusters": clusters}), encoding="utf-8")
    (tmp_path / "out" / "t-live" / "rings.json").write_text(json.dumps({"rings": [{"suspicious": True, "handles": ["gamma", "delta"]}, {"suspicious": False, "handles": ["alpha"]}]}), encoding="utf-8")
    (tmp_path / "out" / "t-live" / "accounts.json").write_text(json.dumps([{"handle": "alpha", "name": "A", "followers": 5000, "reach": 10, "posts": 2, "role": "originator"}]), encoding="utf-8")
    (tmp_path / "out" / "accounts" / "accounts.json").write_text("[]", encoding="utf-8")
    rows = [{"slug": "t-live", "asof": "s1", "kind": "claim", "key": str(i), "gain_24h": 1, "top5_24h": 1 if i in (1, 3, 5, 7) else 0} for i in range(1, 13)]
    reg = {r["handle"]: r for r in registry.build(["t-live"], rows)}
    alpha, gamma = reg["alpha"], reg["gamma"]
    assert alpha["claims_first"] == 6 and alpha["verified"] == 4 and alpha["supported"] == 3 and alpha["reliability"] == 0.75 and alpha["followers"] == 5000
    assert reg["beta"]["claims_first"] == 0 and reg["beta"]["claims_early"] == 6                      # second poster on every odd claim
    assert alpha["early_n"] == 6 and alpha["early_top5"] == 0.67 and alpha["early_lift"] == round(0.67 / (4 / 12), 1)
    assert gamma["reliability"] is None and gamma["early_top5"] == 0.0 and gamma["rings"] == 1 and reg["delta"]["rings"] == 1
    assert registry.lookup()["alpha"]["reliability"] == 0.75
    html_text = registry.table_html(list(reg.values()), 4 / 12)
    assert "@alpha" in html_text and "3 of 4 held up" in html_text and "@beta" in html_text and "@delta" not in html_text   # delta has only a ring


# ------------------------------------------------------------------ Bluesky in the same store
def test_bluesky_posts_share_the_store_with_their_own_threshold(tmp_path):
    from tracker import bsky
    from tracker.store import flatten_bsky, profile_url
    assert bsky.bare_query('"Ken Paxton" since:2026-10-01_00:00:00_UTC until:2026-10-02_00:00:00_UTC min_faves:5 -filter:retweets') == '"Ken Paxton"'
    assert bsky.bare_query("@KenPaxtonTX Talarico") == "KenPaxtonTX Talarico"
    pv = {"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c1",
          "author": {"did": "did:plc:abc", "handle": "jane.bsky.social", "displayName": "Jane", "avatar": "https://cdn/av.jpg", "createdAt": "2024-11-01T00:00:00.000Z"},
          "record": {"$type": "app.bsky.feed.post", "text": "Paxton ducked the debate again", "createdAt": "2026-10-12T15:00:00.000Z", "langs": ["en"],
                     "reply": {"parent": {"uri": "at://did:plc:xyz/app.bsky.feed.post/parent1", "cid": "c0"}, "root": {"uri": "at://did:plc:xyz/app.bsky.feed.post/parent1", "cid": "c0"}},
                     "facets": [{"features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://texastribune.org/story"}]}]},
          "embed": {"$type": "app.bsky.embed.record#view", "record": {"$type": "app.bsky.embed.record#viewRecord", "uri": "at://did:plc:q/app.bsky.feed.post/quoted1", "cid": "c2",
                                                                      "author": {"handle": "reporter.bsky.social"}, "value": {"text": "original"}}},
          "replyCount": 3, "repostCount": 10, "likeCount": 40, "quoteCount": 2, "indexedAt": "2026-10-12T15:00:05.000Z"}
    row = flatten_bsky(pv)
    assert row["id"] == pv["uri"] and row["source"] == "bsky" and row["view_count"] == 0 and row["engagement"] == 55
    assert row["url"] == "https://bsky.app/profile/jane.bsky.social/post/3kxyz" and row["created_at"] == "2026-10-12T15:00:00+00:00"
    assert row["quoted_id"] == "at://did:plc:q/app.bsky.feed.post/quoted1" and row["in_reply_to_id"].endswith("parent1")
    raw = json.loads(row["raw"])
    assert raw["author"]["createdAt"] == "2024-11-01T00:00:00.000Z" and raw["entities"]["urls"] == ["https://texastribune.org/story"] and raw["quoted_tweet"]["author"] == "reporter.bsky.social"
    assert profile_url("jane.bsky.social") == "https://bsky.app/profile/jane.bsky.social" and profile_url("EricLDaugh") == "https://x.com/EricLDaugh" and profile_url("yt:UC123") == "https://www.youtube.com/channel/UC123"
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    st = Store(inc.db_path)
    x = flatten_tweet({"id": "1", "text": "a", "createdAt": "Mon Oct 12 10:00:00 +0000 2026", "viewCount": 1500, "author": {"userName": "h"}})
    st.upsert_posts([x, row, flatten_bsky({**pv, "uri": "at://did:plc:abc/app.bsky.feed.post/small", "likeCount": 2, "repostCount": 0, "replyCount": 0, "quoteCount": 0})])
    assert {r["id"] for r in st.dataset(1000)} == {"1", pv["uri"]}                     # the 2-like Bluesky post is under its threshold
    assert {r["id"] for r in st.unlabeled(1000)} == {"1", pv["uri"]}
    assert st.count("source='bsky'") == 2 and st.count("source='x'") == 1
    st.close()
    # an old store without the columns gets them on open
    import sqlite3
    old = tmp_path / "old.sqlite"
    c = sqlite3.connect(old); c.execute("CREATE TABLE posts (id TEXT PRIMARY KEY, created_at TEXT, view_count INTEGER, like_count INTEGER, retweet_count INTEGER, reply_count INTEGER, quote_count INTEGER, author_handle TEXT, author_name TEXT, author_avatar TEXT, author_followers INTEGER, text TEXT, url TEXT, in_reply_to_id TEXT, quoted_id TEXT, retweeted_id TEXT, in_dataset INTEGER DEFAULT 1, lane TEXT, lane_source TEXT, copy_origin TEXT, raw TEXT)")
    c.execute("INSERT INTO posts (id, created_at, view_count, like_count, retweet_count, reply_count, quote_count, in_dataset) VALUES ('9','2026-10-12T00:00:00+00:00',5000,10,5,1,0,1)"); c.commit(); c.close()
    st2 = Store(old)
    r = st2.conn.execute("SELECT source, engagement FROM posts WHERE id='9'").fetchone()
    assert r["source"] == "x" and r["engagement"] == 16
    st2.close()


# ------------------------------------------------------------------ press: feeds, relevance, lean
def test_news_feeds_store_relevance_and_lean(tmp_path, monkeypatch):
    from tracker import newsfeeds as nf, outlets
    db = tmp_path / "news.sqlite"
    rss = (b'<rss><channel><item><title>Talarico campaign denies report he was treated at Lindner Center</title><link>https://www.texastribune.org/a</link>'
           b'<description>The campaign called the claim false.</description><pubDate>Fri, 09 Oct 2026 20:00:00 GMT</pubDate></item>'
           b'<item><title>Paxton raises $3 million</title><link>https://www.dailywire.com/b</link><pubDate>Fri, 09 Oct 2026 21:00:00 GMT</pubDate></item></channel></rss>')
    class Resp:
        def __init__(self, content, status=200): self.content, self.status_code = content, status
    class Sess:
        headers = {}
        def get(self, url, timeout=0): return Resp(rss) if "ok" in url else Resp(b"", 503)
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text('feeds:\n  - {name: Good, url: "https://ok.example/rss", kind: press}\n  - {name: Dead, url: "https://dead.example/rss", kind: press}\n')
    got = nf.pull_feeds(feeds, db, session=Sess())
    assert got == {"Good": 2, "Dead": 0}
    assert nf.pull_feeds(feeds, db, session=Sess()) == {"Good": 0, "Dead": 0}                  # idempotent
    st = nf.status(db)
    assert st["articles"] == 2 and st["feeds_ok"] == 1 and st["since"] == st["last"]                 # coverage starts at the first fetch, not the oldest item
    claim, ents, subj = "James Talarico was in a mental health facility, the Lindner Center, during his absence.", ["James Talarico", "Lindner Center"], {"talarico", "paxton"}
    from datetime import datetime, timezone
    hits = nf.search(claim, ents, datetime(2026, 10, 1, tzinfo=timezone.utc), db=db, subjects=subj)
    assert [h["domain"] for h in hits] == ["texastribune.org"]                               # the fundraising story is about the race, not the claim
    assert not nf.relevant({"title": "Talarico leads Paxton in new poll"}, claim, ents, subj)
    assert nf.relevant({"title": "Paxton attacks Talarico over mental health facility story"}, claim, ents, subj)
    assert outlets.lean("www.texastribune.org") == "center" and outlets.lean("amp.dailywire.com") == "right" and outlets.lean("nobody.example") == "unrated"
    m = outlets.mix(["texastribune.org", "foxnews.com", "dailywire.com", "nobody.example"])
    assert (m["L"], m["C"], m["R"], m["?"], m["rated"], m["texas"], m["one_sided"]) == (0, 1, 2, 1, 3, 1, "R")
    assert outlets.mix(["nytimes.com", "foxnews.com"])["one_sided"] is None and outlets.mix(["nytimes.com"])["one_sided"] is None


def test_truth_social_archive_items_become_events_and_posts(tmp_path, monkeypatch):
    from tracker import truth, newsfeeds
    monkeypatch.setattr(newsfeeds, "DB_PATH", tmp_path / "news.sqlite")
    rss = (b'<rss version="2.0" xmlns:truth="https://trumpstruth.org/ns/truth" xmlns:archive="https://trumpstruth.org/ns/archive"><channel>'
           b'<item><title>t</title><link>https://www.trumpstruth.org/statuses/42382</link><description>&lt;p&gt;Ken Paxton will WIN Texas! Complete and Total Endorsement.&lt;/p&gt;</description>'
           b'<pubDate>Fri, 16 Oct 2026 05:13:07 +0000</pubDate><truth:originalUrl>https://truthsocial.com/@realDonaldTrump/117448873604871574</truth:originalUrl><archive:postType>original</archive:postType></item>'
           b'<item><title>Video post from October 16, 2026</title><link>https://www.trumpstruth.org/statuses/42390</link><description>&lt;a href="x.mp4"&gt;View video&lt;/a&gt;</description><pubDate>Fri, 16 Oct 2026 13:40:04 +0000</pubDate></item>'
           b'<item><title>t2</title><link>https://www.trumpstruth.org/statuses/42391</link><description>&lt;p&gt;Ohio is doing great.&lt;/p&gt;</description><pubDate>Fri, 16 Oct 2026 14:00:00 +0000</pubDate></item>'
           b'</channel></rss>')
    class Resp:
        content, status_code = rss, 200
    class Sess:
        headers = {}
        def get(self, url, timeout=0): return Resp()
    assert truth.pull(session=Sess()) == 3 and truth.pull(session=Sess()) == 0
    items = truth.parse_items(rss)
    assert items[0]["text"].startswith("Ken Paxton will WIN") and items[0]["original_url"].startswith("https://truthsocial.com/") and items[1]["text"] == ""
    inc = _incident(tmp_path, start="2026-10-10", end="2026-10-17", queries=('"Ken Paxton"', "Paxton Talarico"), lanes_literal="lanes:\n  - {key: x, label: X}")
    rows = truth.rows_for(inc)
    assert len(rows) == 1 and rows[0]["source"] == "truth" and rows[0]["id"] == "truth:42382" and rows[0]["author_handle"] == "realDonaldTrump"
    assert truth.ingest(inc) == 1
    st = Store(inc.db_path)
    assert [r["id"] for r in st.unlabeled(1000)] == ["truth:42382"]                      # above threshold regardless of counts
    st.close()
    monkeypatch.setattr(truth, "_all_posts", lambda: [{"url": "https://www.trumpstruth.org/statuses/1", "original_url": "", "text": "Ken Paxton is a winner", "published": "2026-10-16T05:13:07+00:00", "post_type": "original"}])
    import tracker.config
    monkeypatch.setattr(tracker.config, "incident_paths", lambda: [inc.path])
    truth.auto_events.cache_clear()
    ev = truth.auto_events()
    assert len(ev) == 1 and ev[0]["type"] == "trump" and ev[0]["trackers"] == [inc.slug] and ev[0]["label"].startswith("Trump on Truth Social: Ken Paxton")
    from tracker.store import profile_url
    assert profile_url("realDonaldTrump") == "https://truthsocial.com/@realDonaldTrump"


def test_youtube_videos_flatten_into_posts_and_skip_without_a_key(tmp_path, monkeypatch):
    from tracker import youtube
    v = {"id": "abc123", "snippet": {"publishedAt": "2026-10-12T15:00:00Z", "channelId": "UC9", "channelTitle": "KXAN", "title": "Paxton skips debate",
                                     "description": "Full story.", "thumbnails": {"default": {"url": "https://i/t.jpg"}}, "tags": ["texas"]},
         "statistics": {"viewCount": "12000", "likeCount": "300", "commentCount": "40"}, "contentDetails": {"duration": "PT2M"}}
    row = youtube.flatten_video(v)
    assert row["id"] == "yt:abc123" and row["source"] == "yt" and row["view_count"] == 12000 and row["engagement"] == 340
    assert row["author_handle"] == "yt:UC9" and row["author_name"] == "KXAN" and row["url"].endswith("v=abc123") and row["text"].startswith("Paxton skips debate")
    assert row["created_at"] == "2026-10-12T15:00:00+00:00"
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: X}")
    assert youtube.pull(inc) == 0
    st = Store(inc.db_path); st.upsert_posts([row]); assert [r["id"] for r in st.dataset(1000)] == ["yt:abc123"]; st.close()


def test_officials_registry_matches_posts_and_renders(tmp_path, monkeypatch):
    from tracker import officials as of
    monkeypatch.setattr(of, "OUT_DIR", tmp_path / "out")
    of.officials.cache_clear()
    assert of.is_official("gregabbott_tx")["name"] == "Greg Abbott" and of.is_official("nobody") is None and of.is_official(None) is None
    (tmp_path / "out" / "t-live").mkdir(parents=True)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    posts = [{"id": "1", "handle": "KenPaxtonTX", "t": (now - timedelta(hours=5)).isoformat(), "lane": "a", "views": 50000, "text": "Talarico is hiding", "url": "https://x.com/KenPaxtonTX/status/1"},
             {"id": "2", "handle": "randomguy", "t": (now - timedelta(hours=5)).isoformat(), "lane": "a", "views": 900000, "text": "x", "url": ""},
             {"id": "3", "handle": "jamestalarico", "t": (now - timedelta(days=9)).isoformat(), "lane": "b", "views": 70000, "text": "old", "url": ""}]
    (tmp_path / "out" / "t-live" / "data.json").write_text(json.dumps({"meta": {"title": "T"}, "lanes": [{"key": "a", "label": "Lane A", "color": "#123"}, {"key": "b", "label": "Lane B", "color": "#456"}], "posts": posts}), encoding="utf-8")
    rows = of.said("t-live", days=7)
    assert len(rows) == 1 and rows[0]["name"] == "Ken Paxton" and rows[0]["lane"] == "Lane A" and rows[0]["office"] == "Attorney General"
    ev = of.said_everywhere(14, slugs=["t-live"])
    assert [b["handle"] for b in ev["by_official"]] == ["jamestalarico", "KenPaxtonTX"] and ev["by_official"][0]["top"]["text"] == "old"
    html_text = of.table_html(rows)
    assert "Ken Paxton" in html_text and "Talarico is hiding" in html_text and "50K" in html_text
    of.officials.cache_clear()


def test_message_doc_phrases_are_counted_across_posts_and_press(tmp_path, monkeypatch):
    from tracker import phrases as ph, newsfeeds
    monkeypatch.setattr(newsfeeds, "DB_PATH", tmp_path / "news.sqlite")
    inc = _incident(tmp_path, lanes_literal="lanes:\n  - {key: x, label: Turnaround works}", header='phrases:\n  - "largest turnaround in Texas history"\n  - "93 to 197"\n')
    st = Store(inc.db_path)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    def tweet(i, text, when, handle="h"):
        return flatten_tweet({"id": str(i), "text": text, "createdAt": when.strftime("%a %b %d %H:%M:%S +0000 %Y"), "viewCount": 5000, "author": {"userName": handle, "followers": 10}})
    st.upsert_posts([tweet(1, "HISD is the LARGEST turnaround in Texas history!", now - timedelta(days=1), "GovAbbott"),
                     tweet(2, "largest turnaround... in texas history", now - timedelta(days=2), "booster"),
                     tweet(3, "unrelated", now - timedelta(days=1)),
                     tweet(4, "largest turnaround in Texas history, said nobody", now - timedelta(days=30))])     # outside the window
    st.set_lanes({"1": "x", "2": "x"}, "manual"); st.close()
    conn = newsfeeds.connect(tmp_path / "news.sqlite")
    conn.execute("INSERT INTO articles (url, domain, feed, title, summary, published, fetched_at) VALUES (?,?,?,?,?,?,?)",
                 ("https://city-journal.org/a", "city-journal.org", "CJ", "The largest turnaround in Texas history", "", (now - timedelta(days=3)).isoformat(), now.isoformat()))
    conn.commit(); conn.close()
    data = ph.count(inc)
    r = {x["phrase"]: x for x in data["phrases"]}["largest turnaround in Texas history"]
    assert r["posts"] == 2 and r["accounts"] == 2 and r["articles"] == 1 and r["outlet_list"] == ["city-journal.org"]
    assert r["first_article"]["domain"] == "city-journal.org" and r["first_post"]["handle"] == "booster" and r["lanes"][0][0] == "Turnaround works"
    assert {x["phrase"]: x["posts"] for x in data["phrases"]}["93 to 197"] == 0
    html_text = ph.section_html(data)
    assert "press first" in html_text and "@GovAbbott" in html_text
