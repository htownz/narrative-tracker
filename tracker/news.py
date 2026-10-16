"""News coverage of canonical claims, from the GDELT 2.0 DOC API (free): did a claim cross from X into the
press, when, and did the press have it first?

For each tracker's biggest recent claims (NEWS_TOP per run, at NEWS_MIN_VIEWS+ views, re-checked every
NEWS_RECHECK_H hours) one query is built from the claim's entities plus its most distinctive word, and the
article list for the last 7 days is read: distinct outlets, article counts in the 24 and 48 hours after the
claim first appeared on X, the first article time, and whether coverage preceded the first post.

news(inc) -> rows in news_checks; claims.json / claims.html carry the latest check per claim; the feature table
gets news_sources_48h, which labels outcome O2 (crossover: 3+ outlets within 48h).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import Incident
from .store import Store

API = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWS_TOP = 15
NEWS_MIN_VIEWS = 50_000
NEWS_RECHECK_H = 12
PACE_S = 3.0
STOP = set("""a an the and or but of to in on at for with by from as is are was were be been being this that these those it its he
she they them his her their we you i our your not no do does did have has had will would can could should about into over
than then so if just more most very who whom what which when where why how all any some such own same says said say up out off
down vs after before during while because against between through under again further once here there also texas""".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_checks (
    cluster_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    query TEXT,
    articles INTEGER,
    sources INTEGER,
    articles_24h INTEGER,
    sources_24h INTEGER,
    articles_48h INTEGER,
    sources_48h INTEGER,
    first_news_at TEXT,
    news_before_x INTEGER,
    top_domains TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_cluster ON news_checks(cluster_id, at);
"""


def query_for(text: str, entities: list[str]) -> str:
    """Two entities plus the claim's most distinctive word, each quoted. GDELT needs phrases, not sentences."""
    ents = [e.strip() for e in entities if isinstance(e, str) and 2 < len(e.strip()) <= 40][:2]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text) if w.lower() not in STOP
             and not any(w.lower() in e.lower() for e in ents)]
    words.sort(key=lambda w: (-len(w), w))
    extra = words[:1] if ents else words[:2]
    parts = [f'"{e}"' for e in ents] + [f'"{w}"' for w in extra]
    return " ".join(parts) + " sourcelang:english"


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_articles(query: str, timespan: str = "7d", session: requests.Session | None = None) -> list[dict[str, Any]] | None:
    """Articles matching the query in the timespan, or None when GDELT would not answer (rate limit, outage)."""
    sess = session or requests.Session()
    for attempt in range(3):
        try:
            r = sess.get(API, params={"query": query, "mode": "artlist", "maxrecords": 250, "timespan": timespan, "format": "json", "sort": "datedesc"},
                         timeout=40, headers={"User-Agent": "narrative-tracker/1.0"})
        except requests.exceptions.RequestException as e:
            print(f"  gdelt: {type(e).__name__}; retrying", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            print(f"  gdelt: HTTP {r.status_code} for {query[:60]}", file=sys.stderr)
            return None
        try:
            data = r.json()
        except ValueError:
            return []           # GDELT answers an empty result with an empty body
        return [a for a in (data.get("articles") or []) if isinstance(a, dict)]
    return None


def summarise(articles: list[dict[str, Any]], first_x: datetime) -> dict[str, Any]:
    dated = [(d, a) for a in articles if (d := _parse_date(a.get("seendate", ""))) is not None]
    dated.sort(key=lambda da: da[0])
    def within(hours: float) -> list[dict[str, Any]]:
        return [a for d, a in dated if first_x <= d <= first_x + timedelta(hours=hours)]
    a24, a48 = within(24), within(48)
    domains: dict[str, int] = {}
    for _, a in dated:
        domains[a.get("domain") or "?"] = domains.get(a.get("domain") or "?", 0) + 1
    before = [d for d, _ in dated if d < first_x - timedelta(hours=1)]
    return {
        "articles": len(dated), "sources": len(domains),
        "articles_24h": len(a24), "sources_24h": len({a.get("domain") for a in a24}),
        "articles_48h": len(a48), "sources_48h": len({a.get("domain") for a in a48}),
        "first_news_at": dated[0][0].isoformat() if dated else None,
        "news_before_x": int(bool(before)),
        "top_domains": [d for d, _ in sorted(domains.items(), key=lambda kv: -kv[1])[:6]],
    }


def candidates(store: Store, min_views: int, top: int, recheck_h: float) -> list[dict[str, Any]]:
    """Biggest canonical claims by views, skipping ones checked within recheck_h hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=recheck_h)).isoformat()
    rows = store.conn.execute(
        "SELECT cl.id, cl.text, cl.entities, MIN(p.created_at) first_seen, SUM(p.view_count) views "
        "FROM claim_clusters cl JOIN claims c ON c.cluster_id = cl.id JOIN posts p ON p.id = c.post_id "
        "WHERE cl.id NOT IN (SELECT cluster_id FROM news_checks WHERE at >= ?) "
        "GROUP BY cl.id HAVING views >= ? ORDER BY views DESC LIMIT ?", (since, min_views, top)).fetchall()
    return [dict(r) for r in rows]


def news(inc: Incident, top: int = NEWS_TOP, min_views: int = NEWS_MIN_VIEWS) -> int:
    store = Store(inc.db_path)
    store.conn.executescript(SCHEMA)
    todo = candidates(store, min_views, top, NEWS_RECHECK_H)
    if not todo:
        print("news: nothing to check")
        store.close()
        return 0
    sess = requests.Session()
    done = 0
    for c in todo:
        q = query_for(c["text"], json.loads(c["entities"] or "[]"))
        arts = fetch_articles(q, session=sess)
        if arts is None:
            print("news: GDELT not answering; leaving the rest for next run", file=sys.stderr)
            break
        first_x = datetime.fromisoformat(c["first_seen"]) if c["first_seen"] else datetime.now(timezone.utc)
        s = summarise(arts, first_x)
        store.conn.execute(
            "INSERT INTO news_checks (cluster_id, at, query, articles, sources, articles_24h, sources_24h, articles_48h, sources_48h, first_news_at, news_before_x, top_domains) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(c["id"]), datetime.now(timezone.utc).isoformat(timespec="minutes"), q, s["articles"], s["sources"], s["articles_24h"], s["sources_24h"],
             s["articles_48h"], s["sources_48h"], s["first_news_at"], s["news_before_x"], json.dumps(s["top_domains"])))
        store.conn.commit()
        done += 1
        print(f"  news {s['sources_48h']:>2} outlets/48h {s['articles']:>3} articles{' (press first)' if s['news_before_x'] else ''}  {c['text'][:80]}")
        time.sleep(PACE_S)
    store.close()
    print(f"news: {done} claims checked ({len(todo) - done} left)")
    return done


def latest_checks(store: Store) -> dict[int, dict[str, Any]]:
    """The most recent news check per canonical claim."""
    if not store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='news_checks'").fetchone():
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in store.conn.execute("SELECT * FROM news_checks ORDER BY at"):
        d = dict(r)
        d["top_domains"] = json.loads(d.get("top_domains") or "[]")
        out[int(d["cluster_id"])] = d
    return out
