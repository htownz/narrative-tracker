"""Trump's Truth Social posts, from the trumpstruth.org public archive feed (RSS), as two things:

  1. calendar events of type "trump" on every tracker whose subject the post names (an endorsement or an
     attack from the President is an event the lanes respond to, like a poll or a rally);
  2. posts in those trackers' stores (source "truth", always above threshold — there are only a few a day
     that mention Texas), so the claims extractor sees them and a claim that started on Truth Social and
     was screenshotted onto X shows "Truth Social first" with the lag.

Truth Social has no official API; the archive is the clean route and covers one account, which is the one
that matters for a Texas race. Items are kept in data/news.sqlite (table truth_posts), so history outlives
the feed's ~50-item window.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import requests

from .config import Incident
from .newsfeeds import _parse_date, _strip_html, connect, parse_feed

FEED = "https://trumpstruth.org/feed"
HANDLE = "realDonaldTrump"
SCHEMA = """
CREATE TABLE IF NOT EXISTS truth_posts (
    url TEXT PRIMARY KEY,
    original_url TEXT,
    text TEXT,
    published TEXT,
    post_type TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_truth_published ON truth_posts(published);
"""


def _conn() -> sqlite3.Connection:
    conn = connect()
    conn.executescript(SCHEMA)
    return conn


def parse_items(content: bytes) -> list[dict[str, Any]]:
    """Feed items -> {url, original_url, text, published, post_type}; video-only posts keep their title as text."""
    import xml.etree.ElementTree as ET
    base = parse_feed(content)
    extra: dict[str, dict[str, str]] = {}
    try:
        root = ET.fromstring(content)
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            d = {}
            for el in it:
                tag = el.tag.split("}")[-1]
                if tag in ("originalUrl", "postType") and el.text:
                    d[tag] = el.text.strip()
            extra[link] = d
    except ET.ParseError:
        pass
    out = []
    for it in base:
        text = re.sub(r"\s*(View video|View image)\s*$", "", _strip_html(it["summary"])).strip()
        if not text and not re.match(r"^(Video|Image|Media) post from", it["title"]):
            text = it["title"].strip()                       # a media-only post keeps no text; a reply/quote title is the text
        out.append({"url": it["url"], "original_url": extra.get(it["url"], {}).get("originalUrl", ""), "text": text,
                    "published": it["published"], "post_type": extra.get(it["url"], {}).get("postType", "original")})
    return out


def pull(session: requests.Session | None = None) -> int:
    """Fetch the archive feed and keep new items. Returns the number of new posts stored."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; narrative-tracker/1.0; +https://github.com/htownz/narrative-tracker)")
    try:
        r = sess.get(FEED, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"truth social: {type(e).__name__}; skipped")
        return 0
    if r.status_code != 200:
        print(f"truth social: HTTP {r.status_code}; skipped")
        return 0
    items = parse_items(r.content)
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    new = 0
    for it in items:
        d = _parse_date(it["published"])
        cur = conn.execute("INSERT OR IGNORE INTO truth_posts (url, original_url, text, published, post_type, fetched_at) VALUES (?,?,?,?,?,?)",
                           (it["url"], it["original_url"], it["text"][:2000], d.isoformat() if d else now, it["post_type"], now))
        new += 1 if cur.rowcount > 0 else 0
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM truth_posts").fetchone()[0]
    conn.close()
    print(f"truth social: {len(items)} items in the feed, {new} new, {total} stored")
    auto_events.cache_clear()
    return new


def _all_posts() -> list[dict[str, Any]]:
    try:
        conn = _conn()
    except sqlite3.Error:
        return []
    rows = [dict(r) for r in conn.execute("SELECT * FROM truth_posts WHERE text != '' ORDER BY published")]
    conn.close()
    return rows


def mentions(text: str, slug: str, subjects: set[str]) -> bool:
    t = text.lower()
    if any(s_ in t for s_ in subjects):
        return True
    return slug.startswith("trump") and "texas" in t


@lru_cache(maxsize=1)
def auto_events() -> tuple[dict[str, Any], ...]:
    """Trump posts that name a tracker's subject, as events (computed once per process)."""
    from .config import incident_paths, load_incident
    from .pollevents import subject_tokens
    subjects: dict[str, set[str]] = {}
    for path in incident_paths():
        try:
            inc = load_incident(path)
            subjects[inc.slug] = subject_tokens(inc.queries)
        except Exception:
            continue
    out = []
    for p in _all_posts():
        trackers = sorted(slug for slug, subj in subjects.items() if mentions(p["text"], slug, subj))
        if not trackers:
            continue
        label = re.sub(r"\s+", " ", p["text"])[:70].rstrip(" ,.;:") + ("…" if len(p["text"]) > 70 else "")
        out.append({"label": f"Trump on Truth Social: {label}", "type": "trump", "at": p["published"], "all_day": False,
                    "source": f"@{HANDLE} on Truth Social, via the trumpstruth.org archive", "auto": True, "trackers": trackers,
                    "detail": p["text"][:160], "url": p["original_url"] or p["url"]})
    return tuple(out)


def rows_for(inc: Incident) -> list[dict[str, Any]]:
    """Trump posts that belong in this tracker's store, as posts rows (source 'truth')."""
    from .pollevents import subject_tokens
    subj = subject_tokens(inc.queries)
    start = datetime.fromisoformat(inc.start).replace(tzinfo=timezone.utc).isoformat()
    rows = []
    for p in _all_posts():
        if not mentions(p["text"], inc.slug, subj) or p["published"] < start:
            continue
        rows.append({
            "id": f"truth:{p['url'].rsplit('/', 1)[-1]}", "created_at": p["published"], "view_count": 0, "like_count": 0, "retweet_count": 0,
            "reply_count": 0, "quote_count": 0, "author_handle": HANDLE, "author_name": "Donald J. Trump", "author_avatar": "",
            "author_followers": 0, "text": p["text"], "url": p["original_url"] or p["url"], "in_reply_to_id": None, "quoted_id": None,
            "retweeted_id": None, "in_dataset": 1,
            "raw": json.dumps({"_slim": 1, "_src": "truth", "author": {"userName": HANDLE, "createdAt": "2022-02-21T00:00:00+00:00", "followers": 0},
                               "entities": {"urls": [], "hashtags": []}, "post_type": p["post_type"]}),
            "source": "truth", "engagement": 0,
        })
    return rows


def ingest(inc: Incident) -> int:
    """Upsert this tracker's Trump posts into its store. Returns how many."""
    from .store import Store
    rows = rows_for(inc)
    if not rows:
        return 0
    st = Store(inc.db_path)
    st.upsert_posts(rows)
    st.close()
    print(f"truth social: {len(rows)} Trump posts in this tracker's window")
    return len(rows)


if __name__ == "__main__":
    pull()
    for e in auto_events()[-10:]:
        print(e["at"][:16], e["trackers"], e["label"])
