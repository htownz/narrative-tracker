"""Bluesky: the same tracker queries against the public AppView search, into the same store.

Why: it is where much of the Texas Democratic and press-adjacent conversation moved, it is free and official
(no key, no scraping), and it lets a canonical claim be seen on two platforms with a timestamp on each.

The public AppView (public.api.bsky.app) answers app.bsky.feed.searchPosts without authentication: q with
quoted phrases, since/until as ISO datetimes, sort=latest, up to 100 per page with a cursor. Datacenter
addresses (GitHub's runners among them) can get a 403 from it, so when BSKY_HANDLE and BSKY_APP_PASSWORD
are set the pull signs in (com.atproto.server.createSession with an app password — never the account
password) and makes the same calls through bsky.social with a bearer token instead. The X query
operators (since:/until:/min_faves:/-filter:) are not sent; the bare query is. No view counts exist on
Bluesky, so view_count is 0 and the engagement column (likes + reposts + replies + quotes) carries the
threshold (store.BSKY_MIN_ENGAGEMENT). Quoted and replied-to posts are fetched with getPosts (25 per call).

Day-by-day, resumable with the same pull_progress meta the X pull uses (keys prefixed bsky:). Set
`bluesky: false` in an incident YAML to leave a tracker X-only.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests

from .config import Incident
from .store import Store, flatten_bsky

APPVIEW = "https://public.api.bsky.app/xrpc"
PDS = "https://bsky.social/xrpc"
SEARCH = "app.bsky.feed.searchPosts"
GET_POSTS = "app.bsky.feed.getPosts"
CREATE_SESSION = "com.atproto.server.createSession"
PAGE = 100
PAUSE_S = 0.35               # the public AppView allows roughly 3,000 requests per 5 minutes per address; stay far under
MAX_PAGES_PER_DAY = 30       # 3,000 posts for one query on one day is plenty


def log(msg: str) -> None:
    print(msg, flush=True)


def bare_query(q: str) -> str:
    """The incident query without X-only operators; @handles become plain words (Bluesky handles differ)."""
    q = re.sub(r"\s+(since|until|min_faves|min_retweets|lang|filter):\S+", "", q)
    q = re.sub(r"\s+-filter:\S+", "", q)
    q = re.sub(r"(^|\s)@(\w+)", r"\1\2", q)
    return " ".join(q.split())


def day_bounds_iso(day: date, tz: str) -> tuple[str, str]:
    z = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=z).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Bluesky:
    def __init__(self, pause: float = PAUSE_S, handle: str | None = None, app_password: str | None = None):
        import os
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = "Mozilla/5.0 (compatible; narrative-tracker/1.0; +https://github.com/htownz/narrative-tracker)"
        self.pause = pause
        self.calls = 0
        self.base = APPVIEW
        handle = handle or os.environ.get("BSKY_HANDLE")
        app_password = app_password or os.environ.get("BSKY_APP_PASSWORD")
        self.signed_in = False
        if handle and app_password:
            r = self.sess.post(f"{PDS}/{CREATE_SESSION}", json={"identifier": handle, "password": app_password}, timeout=40)
            if r.status_code == 200 and r.json().get("accessJwt"):
                self.sess.headers["Authorization"] = f"Bearer {r.json()['accessJwt']}"
                self.base = PDS
                self.signed_in = True
            else:
                log(f"bluesky: sign-in failed (HTTP {r.status_code}); using the public endpoint")

    def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}/{method}"
        last: Exception | None = None
        for attempt in range(5):
            try:
                r = self.sess.get(url, params=params, timeout=40)
            except requests.exceptions.RequestException as e:
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            self.calls += 1
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code != 200:
                body = r.text[:160].replace("\n", " ")
                raise RuntimeError(f"bluesky HTTP {r.status_code} from {url.split('/xrpc')[0]}{' (signed in)' if self.signed_in else ' (public, no sign-in)'}: {body}")
            time.sleep(self.pause)
            return r.json()
        raise RuntimeError(f"bluesky: gave up ({last})")

    def search_pages(self, q: str, since: str, until: str, cursor: str = "") -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        pages = 0
        while True:
            params: dict[str, Any] = {"q": q, "sort": "latest", "since": since, "until": until, "limit": PAGE}
            if cursor:
                params["cursor"] = cursor
            data = self._get(SEARCH, params)
            posts = [p for p in (data.get("posts") or []) if isinstance(p, dict) and p.get("uri")]
            cursor = data.get("cursor") or ""
            pages += 1
            done = not cursor or not posts or pages >= MAX_PAGES_PER_DAY
            yield posts, (None if done else cursor)
            if done:
                return

    def get_posts(self, uris: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(uris), 25):
            data = self._get(GET_POSTS, {"uris": uris[i:i + 25]})
            out += [p for p in (data.get("posts") or []) if isinstance(p, dict) and p.get("uri")]
        return out


def _rows_from(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [flatten_bsky(p, in_dataset=1) for p in posts]


def day_queries(inc: Incident) -> list[tuple[date, str, str, str]]:
    start, end = date.fromisoformat(inc.start), date.fromisoformat(inc.end)
    out = []
    day = start
    while day <= end:
        since, until = day_bounds_iso(day, inc.timezone)
        for q in inc.queries:
            out.append((day, bare_query(q), since, until))
        day += timedelta(days=1)
    return out


def pull(inc: Incident, budget: float | None = None, dry_run: bool = False) -> bool:
    """Returns True when complete, False when it stopped on the time budget."""
    if inc.raw.get("bluesky") is False:
        log("bluesky: off for this tracker")
        return True
    plan = day_queries(inc)
    log(f"bluesky: {len(plan)} day-queries planned ({len(inc.queries)} queries x {len(plan) // max(1, len(inc.queries))} days)")
    if dry_run:
        for _, q, since, until in plan[:5]:
            log(f"   {q}  {since}..{until}")
        return True
    t0 = time.time()
    api = Bluesky()
    log(f"bluesky: {'signed in as ' + __import__('os').environ.get('BSKY_HANDLE', '') if api.signed_in else 'public endpoint (set BSKY_HANDLE / BSKY_APP_PASSWORD to sign in)'}")
    store = Store(inc.db_path)
    progress: dict[str, Any] = store.get_meta("bsky_progress", {}) or {}
    if inc.live:
        fresh = date.fromisoformat(inc.end) - timedelta(days=inc.refresh_days - 1)
        for day, q, since, until in plan:
            key = f"{q}|{since}"
            if day >= fresh and progress.get(key):
                progress[key] = {}
        store.set_meta("bsky_refs_fetched", False)

    def out_of_time() -> bool:
        return budget is not None and (time.time() - t0) > budget

    complete, n_new = True, 0
    for day, q, since, until in plan:
        key = f"{q}|{since}"
        st = progress.get(key) or {}
        if st.get("done"):
            continue
        if out_of_time():
            complete = False
            break
        cursor, rows_n = st.get("cursor") or "", int(st.get("rows") or 0)
        try:
            for posts, nxt in api.search_pages(q, since, until, cursor=cursor):
                rows = _rows_from(posts)
                store.upsert_posts(rows)
                rows_n += len(rows)
                n_new += len(rows)
                progress[key] = {"done": nxt is None, "cursor": nxt or "", "rows": rows_n}
                store.set_meta("bsky_progress", progress)
                if nxt is None or out_of_time():
                    complete = complete and nxt is None
                    break
        except RuntimeError as e:
            if "HTTP 403" in str(e) or "HTTP 401" in str(e):   # blocked or not signed in: nothing else will answer either
                log(f"bluesky: {e}")
                log("bluesky: stopping this run; set BSKY_HANDLE and BSKY_APP_PASSWORD (GitHub secrets) to sign in")
                complete = False
                break
            log(f"  [skip] bluesky {q[:50]} {since[:10]}: {e}")
            progress[key] = {"done": True, "cursor": "", "rows": rows_n, "error": str(e)[:200]}
            store.set_meta("bsky_progress", progress)
        if not progress.get(key, {}).get("done"):
            break

    if complete and inc.fetch_referenced and not store.get_meta("bsky_refs_fetched"):
        have = store.ids()
        wanted = sorted({r[c] for r in store.conn.execute("SELECT in_reply_to_id, quoted_id FROM posts WHERE source='bsky' AND in_dataset=1")
                         for c in ("in_reply_to_id", "quoted_id") if r[c] and r[c].startswith("at://") and r[c] not in have})
        if wanted and not out_of_time():
            log(f"bluesky: fetching {len(wanted)} referenced posts")
            try:
                fetched = api.get_posts(wanted)
                store.upsert_posts([flatten_bsky(p, in_dataset=0) for p in fetched])
            except RuntimeError as e:
                log(f"  bluesky refs: {e}")
        store.set_meta("bsky_refs_fetched", True)

    n_b = store.count("source='bsky' AND in_dataset=1")
    n_thr = store.count("source='bsky' AND in_dataset=1 AND engagement >= 25")
    done = sum(1 for _, q, since, _u in plan if progress.get(f"{q}|{since}", {}).get("done"))
    log(f"bluesky {'done' if complete else 'paused on budget'}: {done}/{len(plan)} day-queries, {n_b} posts in window ({n_thr} at 25+ engagement), "
        f"{n_new} rows this run, {api.calls} calls, {time.time() - t0:.0f}s")
    store.set_meta("bsky_pulled_at", datetime.now(timezone.utc).isoformat())
    store.close()
    return complete


def probe(q: str = '"Ken Paxton"') -> None:
    """Print the raw shape of one search result (for checking the AppView's fields from a machine with network)."""
    import json
    api = Bluesky()
    print("signed in" if api.signed_in else "public endpoint")
    data = api._get(SEARCH, {"q": q, "sort": "latest", "limit": 2})
    print(json.dumps(data, indent=1)[:6000])
    posts = data.get("posts") or []
    if posts:
        print("\nflattened:", json.dumps(flatten_bsky(posts[0]), indent=1)[:2000])


if __name__ == "__main__":
    probe(sys.argv[1] if len(sys.argv) > 1 else '"Ken Paxton"')
