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
