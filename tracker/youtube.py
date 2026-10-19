"""YouTube: the tracker queries against the Data API v3, once a day, into the same store (source "yt").

Why: candidate channels, local TV clips, podcast appearances and ad uploads live here with real view counts,
so a YouTube video is a post like any other for lanes, claims and control. The official API is free with a
10,000-unit daily quota; a search costs 100 units, so each tracker searches once per query per day
(QUOTA_SEARCHES_PER_DAY caps it) and the pull runs at most once per PULL_EVERY_H hours per tracker.

Needs YOUTUBE_API_KEY (Google Cloud console -> enable "YouTube Data API v3" -> Credentials -> API key).
Without it the step logs one line and skips.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import Incident
from .store import Store, parse_created_at

API = "https://www.googleapis.com/youtube/v3"
PULL_EVERY_H = 40            # every other day per tracker: 26 trackers x 4 queries x 100 units / 2 days fits the 10,000-unit day
PAGES_FIRST_RUN = 2          # 100 newest videos per query on the first pull; 50 per pull after
MAX_QUERIES = 4              # the first four queries of a tracker (the broadest come first)
QUOTA_SEARCHES_PER_DAY = 60  # 6,000 units, leaving room for the statistics calls


def bare_query(q: str) -> str:
    from .bsky import bare_query as bq
    return bq(q)


def flatten_video(v: dict[str, Any], in_dataset: int = 1) -> dict[str, Any]:
    """videos.list item (snippet + statistics) -> posts row."""
    sn, st = v.get("snippet") or {}, v.get("statistics") or {}
    vid = str(v.get("id") if isinstance(v.get("id"), str) else (v.get("id") or {}).get("videoId") or "")
    title, desc = sn.get("title") or "", (sn.get("description") or "")[:500]
    likes, comments = int(st.get("likeCount") or 0), int(st.get("commentCount") or 0)
    return {
        "id": f"yt:{vid}", "created_at": parse_created_at(sn.get("publishedAt")), "view_count": int(st.get("viewCount") or 0),
        "like_count": likes, "retweet_count": 0, "reply_count": comments, "quote_count": 0,
        "author_handle": f"yt:{sn.get('channelId') or ''}", "author_name": sn.get("channelTitle") or "",
        "author_avatar": ((sn.get("thumbnails") or {}).get("default") or {}).get("url") or "", "author_followers": 0,
        "text": f"{title}\n{desc}".strip(), "url": f"https://www.youtube.com/watch?v={vid}",
        "in_reply_to_id": None, "quoted_id": None, "retweeted_id": None, "in_dataset": in_dataset,
        "raw": json.dumps({"_slim": 1, "_src": "yt", "id": vid, "createdAt": sn.get("publishedAt"), "channelId": sn.get("channelId"),
                           "author": {"userName": f"yt:{sn.get('channelId') or ''}", "name": sn.get("channelTitle"), "createdAt": None, "followers": 0},
                           "entities": {"urls": [], "hashtags": sn.get("tags") or []}, "duration": (v.get("contentDetails") or {}).get("duration")}, ensure_ascii=False),
        "source": "yt", "engagement": likes + comments,
    }


class YouTube:
    def __init__(self, key: str):
        self.key = key
        self.sess = requests.Session()
        self.units = 0

    def _get(self, path: str, params: dict[str, Any], cost: int) -> dict[str, Any]:
        for attempt in range(3):
            try:
                r = self.sess.get(f"{API}/{path}", params={**params, "key": self.key}, timeout=40)
            except requests.exceptions.RequestException:
                time.sleep(5 * (attempt + 1))
                continue
            self.units += cost
            if r.status_code == 403 and "quota" in r.text.lower():
                raise RuntimeError("youtube: daily quota exhausted")
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"youtube HTTP {r.status_code}: {r.text[:160]}")
            return r.json()
        raise RuntimeError("youtube: no answer")

    def search(self, q: str, published_after: str, pages: int = 1) -> list[str]:
        ids, token = [], ""
        for _ in range(pages):
            params = {"part": "snippet", "q": q, "type": "video", "order": "date", "maxResults": 50, "publishedAfter": published_after,
                      "relevanceLanguage": "en", "regionCode": "US"}
            if token:
                params["pageToken"] = token
            data = self._get("search", params, 100)
            ids += [it["id"]["videoId"] for it in data.get("items") or [] if (it.get("id") or {}).get("videoId")]
            token = data.get("nextPageToken") or ""
            if not token:
                break
        return ids

    def videos(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50]), "maxResults": 50}, 1)
            out += data.get("items") or []
        return out


def pull(inc: Incident, force: bool = False) -> int:
    """Search each query for videos since the last pull (or the window start), store them with statistics.
    Returns rows stored; 0 when skipped."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("youtube: YOUTUBE_API_KEY not set; skipped")
        return 0
    if inc.raw.get("youtube") is False:
        print("youtube: off for this tracker")
        return 0
    store = Store(inc.db_path)
    last = store.get_meta("yt_pulled_at")
    if last and not force and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=PULL_EVERY_H):
        print(f"youtube: pulled {last[:16]}; next after {PULL_EVERY_H}h")
        store.close()
        return 0
    from zoneinfo import ZoneInfo
    window_start = datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc)
    since = max(window_start, datetime.fromisoformat(last) - timedelta(days=2)) if last else window_start
    api = YouTube(key)
    ids: list[str] = []
    searches = 0
    try:
        for q in inc.queries[:MAX_QUERIES]:
            if searches >= QUOTA_SEARCHES_PER_DAY:
                break
            pages = 1 if last else PAGES_FIRST_RUN
            got = api.search(bare_query(q), since.strftime("%Y-%m-%dT%H:%M:%SZ"), pages=pages)
            searches += pages
            ids += got
        ids = list(dict.fromkeys(ids))
        # re-read statistics for videos already stored from the last 7 days (view counts move), 1 unit per 50
        recent = [r["id"][3:] for r in store.conn.execute("SELECT id FROM posts WHERE source='yt' AND created_at >= ?",
                                                           ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))]
        ids = list(dict.fromkeys(ids + recent))
        vids = api.videos(ids) if ids else []
    except RuntimeError as e:
        print(f"youtube: {e}", file=sys.stderr)
        store.close()
        return 0
    rows = [flatten_video(v) for v in vids]
    rows = [r for r in rows if r["created_at"] and r["created_at"] >= window_start.isoformat()]
    store.upsert_posts(rows)
    store.set_meta("yt_pulled_at", datetime.now(timezone.utc).isoformat())
    n_thr = store.count(f"source='yt' AND view_count >= {inc.min_views}")
    print(f"youtube: {len(rows)} videos stored ({n_thr} at {inc.min_views}+ views in the store), {searches} searches, ~{api.units} quota units")
    store.close()
    return len(rows)
