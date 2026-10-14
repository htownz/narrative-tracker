"""Pull posts from twitterapi.io for an incident window.

Cost model (twitterapi.io, Oct 2026): 15 credits ($0.00015) per tweet returned by advanced search,
15-credit minimum per call. A 1,500-post incident with reference fetches lands well under $1.

Rate limits: free tier (no recharge balance) is one request every 5 seconds; the client adapts when
it sees that 429. Progress (per-query cursor) is saved in the store after every page, so an interrupted
pull resumes where it stopped. `--budget N` stops cleanly after N seconds for shells with a time cap.
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests

from .config import Incident, env
from .store import Store, flatten_tweet

BASE = "https://api.twitterapi.io"
SEARCH = f"{BASE}/twitter/tweet/advanced_search"
LOOKUP = f"{BASE}/twitter/tweets"
FREE_TIER_PAUSE = 5.2


def log(msg: str) -> None:
    print(msg, flush=True)


class TwitterAPI:
    def __init__(self, api_key: str, pause: float = 0.2):
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Accept": "application/json"})
        self.pause = pause
        self.calls = 0
        self.tweets_returned = 0
        self._last = 0.0

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(8):
            wait = self.pause - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self.session.get(url, params=params, timeout=60)
            self._last = time.time()
            self.calls += 1
            if r.status_code == 429:
                body = r.text.lower()
                if "free-tier" in body or "every 5 seconds" in body:
                    if self.pause < FREE_TIER_PAUSE:
                        log(f"  free-tier rate limit detected: pacing to one call per {FREE_TIER_PAUSE}s")
                        self.pause = FREE_TIER_PAUSE
                    continue
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 401 or r.status_code == 403:
                sys.exit(f"twitterapi.io refused the key ({r.status_code}): {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"twitterapi.io kept failing: {url} {params}")

    def search_pages(self, query: str, query_type: str = "Latest", cursor: str = "") -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield (tweets, next_cursor) per page; next_cursor is None on the last page."""
        while True:
            data = self._get(SEARCH, {"query": query, "queryType": query_type, "cursor": cursor or ""})
            tweets = data.get("tweets") or []
            self.tweets_returned += len(tweets)
            nxt = data.get("next_cursor") if data.get("has_next_page") else None
            yield tweets, nxt
            if not nxt:
                return
            cursor = nxt

    def lookup(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):   # the lookup endpoint 400s above ~50 ids
            chunk = ids[i:i + 50]
            data = self._get(LOOKUP, {"tweet_ids": ",".join(chunk)})
            tweets = data.get("tweets") or []
            self.tweets_returned += len(tweets)
            out.extend(tweets)
        return out

    @property
    def est_cost_usd(self) -> float:
        credits = max(self.tweets_returned, self.calls) * 15
        return credits / 100_000


def day_bounds_utc(day: date, tz: str) -> tuple[str, str]:
    """Local-day start/end in the since:/until: format X search understands (UTC)."""
    z = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=z)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%d_%H:%M:%S_UTC"
    return start.astimezone(ZoneInfo("UTC")).strftime(fmt), end.astimezone(ZoneInfo("UTC")).strftime(fmt)


def build_queries(inc: Incident) -> list[str]:
    return [q for _, q in build_day_queries(inc)]


def build_day_queries(inc: Incident) -> list[tuple[date, str]]:
    """(local day, full search string) for every query x day in the window."""
    start = date.fromisoformat(inc.start)
    end = date.fromisoformat(inc.end)
    out: list[tuple[date, str]] = []
    day = start
    while day <= end:
        since, until = day_bounds_utc(day, inc.timezone)
        for q in inc.queries:
            parts = [q, f"since:{since}", f"until:{until}"]
            if inc.min_faves_hint:
                parts.append(f"min_faves:{inc.min_faves_hint}")
            if inc.exclude_retweets:
                parts.append("-filter:retweets")
            out.append((day, " ".join(parts)))
        day += timedelta(days=1)
    return out


def _rows_from(tweets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    batch = []
    for t in tweets:
        if not t.get("id"):
            continue
        batch.append(flatten_tweet(t, in_dataset=1))
        for key in ("quoted_tweet", "retweeted_tweet"):
            if t.get(key) and t[key].get("id"):
                batch.append(flatten_tweet(t[key], in_dataset=0))
    return batch


def pull(inc: Incident, dry_run: bool = False, budget: float | None = None, pause: float | None = None) -> bool:
    """Returns True when the pull is complete, False when it stopped on the time budget."""
    api_key = env("TWITTERAPI_IO_KEY")
    day_queries = build_day_queries(inc)
    queries = [q for _, q in day_queries]
    log(f"{len(queries)} search queries planned ({len(inc.queries)} queries x {len(day_queries) // max(1, len(inc.queries))} days, "
        f"{inc.start} to {inc.end}{' live' if inc.live else ''})")
    if dry_run:
        for q in queries:
            log("   " + q)
        return True
    if not api_key:
        sys.exit("TWITTERAPI_IO_KEY is not set (env var or .env). Get one at https://twitterapi.io/dashboard")

    t_start = time.time()
    api = TwitterAPI(api_key, pause=pause if pause is not None else float(env("TWITTERAPI_PAUSE", "0.2") or 0.2))
    store = Store(inc.db_path)
    progress: dict[str, Any] = store.get_meta("pull_progress", {}) or {}

    if inc.live:
        # Re-pull the most recent days every run: new posts land, and view counts for those days refresh.
        # Older days keep the counts from when they were first pulled (same convention as the original chart).
        fresh = date.fromisoformat(inc.end) - timedelta(days=inc.refresh_days - 1)
        reset = 0
        for day, q in day_queries:
            if day >= fresh and progress.get(q, {}).get("done"):
                progress[q] = {}
                reset += 1
        if reset:
            log(f"live mode: re-pulling {reset} queries covering the last {inc.refresh_days} days")
        store.set_meta("refs_fetched", False)

    done_before = sum(1 for q in queries if progress.get(q, {}).get("done"))
    if done_before:
        log(f"resuming: {done_before}/{len(queries)} queries already complete")

    def out_of_time() -> bool:
        return budget is not None and (time.time() - t_start) > budget

    complete = True
    for qi, q in enumerate(queries, 1):
        st = progress.get(q) or {}
        if st.get("done"):
            continue
        if out_of_time():
            complete = False
            break
        cursor = st.get("cursor") or ""
        pages = int(st.get("pages") or 0)
        n_rows = int(st.get("rows") or 0)
        for tweets, nxt in api.search_pages(q, inc.query_type, cursor=cursor):
            rows = _rows_from(tweets)
            store.upsert_posts(rows)
            pages += 1
            n_rows += len(rows)
            progress[q] = {"done": nxt is None, "cursor": nxt or "", "pages": pages, "rows": n_rows}
            store.set_meta("pull_progress", progress)
            if nxt is None:
                break
            if out_of_time():
                complete = False
                break
        log(f"  [{qi}/{len(queries)}] {q[:58]:<58} pages={pages:<3} rows={n_rows:<4} "
            f"{'done' if progress[q]['done'] else 'PAUSED'}  (calls {api.calls}, est ${api.est_cost_usd:.3f})")
        if not progress[q]["done"]:
            break

    if complete and inc.fetch_referenced and not store.get_meta("refs_fetched"):
        missing = sorted(store.missing_reference_ids())
        if missing:
            if out_of_time():
                complete = False
            else:
                log(f"fetching {len(missing)} referenced posts (replied-to parents), {-(-len(missing) // 50)} calls")
                fetched = api.lookup(missing)
                store.upsert_posts([flatten_tweet(t, in_dataset=0) for t in fetched])
                store.set_meta("refs_fetched", True)
        else:
            store.set_meta("refs_fetched", True)

    prev_calls = int(store.get_meta("api_calls", 0) or 0)
    prev_cost = float(store.get_meta("est_cost_usd", 0) or 0)
    store.set_meta("api_calls", prev_calls + api.calls)
    store.set_meta("est_cost_usd", round(prev_cost + api.est_cost_usd, 4))
    if complete:
        store.set_meta("pulled_at", datetime.utcnow().isoformat())
        store.set_meta("synthetic", False)
    n_in = store.count("in_dataset=1")
    n_thr = store.count(f"in_dataset=1 AND view_count >= {inc.min_views}")
    n_done = sum(1 for q in queries if progress.get(q, {}).get("done"))
    log(f"{'done' if complete else 'paused on budget'}: {n_done}/{len(queries)} queries, {n_in} unique in-window posts, "
        f"{n_thr} at {inc.min_views}+ views, {api.calls} calls this run, est ${prev_cost + api.est_cost_usd:.3f} total, "
        f"{(time.time() - t_start):.0f}s")
    store.close()
    return complete
