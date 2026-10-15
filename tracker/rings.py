"""Link rings: the same link pushed by many accounts inside a short window.

Same-words detection (dedup.py) catches copied captions. Paid or organized distribution more often shares the
asset — an article, a clip, a post's URL — with different captions. This looks at every stored post's links
(raw.entities.urls, kept by store.slim_raw), groups them by normalized URL and flags a ring when at least
MIN_ACCOUNTS distinct accounts shared the same link inside WINDOW_H hours. Organic news sharing spreads
over days; a ring lands at once.

rings(inc) -> out/<slug>/rings.json  {rings: [...], stats}; analysis.html shows them, control.py scores the
share of a lane's posts that sit in a ring, and the weekly brief lists the biggest.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Incident
from .control import account_age_days
from .store import Store

MIN_ACCOUNTS = 5
WINDOW_H = 2.0
SMALL_FOLLOWERS = 5_000
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "igshid", "ref", "ref_src",
            "s", "t", "si", "feature", "smid", "sref", "ncid", "cmpid", "mc_cid", "mc_eid", "sh", "taid", "ito"}
KEEP_QUERY = {"youtube.com": {"v"}, "youtu.be": set(), "m.youtube.com": {"v"}}


def normalize(url: str) -> str | None:
    """Strip tracking parameters, fragments, scheme/www differences; drop unexpanded t.co links."""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return None
    host = (s.netloc or "").lower().removeprefix("www.")
    if not host or host == "t.co":
        return None
    if host in ("twitter.com", "mobile.twitter.com"):
        host = "x.com"
    keep = KEEP_QUERY.get(host)
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=False)
         if (keep is not None and k in keep) or (keep is None and k.lower() not in TRACKING and not k.lower().startswith("utm_"))]
    path = s.path.rstrip("/") or "/"
    if host == "x.com":
        m = re.search(r"/status/(\d+)", path)
        if m:
            return f"x.com/status/{m.group(1)}"
    return urlunsplit(("", host, path, urlencode(sorted(q)), "")).lstrip("/")


def _urls(post: dict[str, Any]) -> list[str]:
    try:
        raw = json.loads(post.get("raw") or "{}")
    except (TypeError, ValueError):
        return []
    out = []
    for u in (raw.get("entities") or {}).get("urls") or []:
        if isinstance(u, str):
            n = normalize(u)
            if n and n not in out:
                out.append(n)
    return out


def _tightest(times: list[float], window_s: float) -> tuple[int, float]:
    """Most distinct timestamps inside any window_s span, and that span's start."""
    times = sorted(times)
    best, best_start, j = 0, times[0] if times else 0.0, 0
    for i, t in enumerate(times):
        while times[j] < t - window_s:
            j += 1
        if i - j + 1 > best:
            best, best_start = i - j + 1, times[j]
    return best, best_start


def find_rings(posts: list[dict[str, Any]], min_accounts: int = MIN_ACCOUNTS, window_h: float = WINDOW_H) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in posts:
        if not p.get("created_at"):
            continue
        for u in _urls(p):
            groups[u].append(p)
    rings = []
    for url, ps in groups.items():
        by_acc: dict[str, dict[str, Any]] = {}
        for p in sorted(ps, key=lambda q: q["created_at"]):
            by_acc.setdefault(p["author_handle"], p)          # each account's first share of the link
        if len(by_acc) < min_accounts:
            continue
        firsts = {h: datetime.fromisoformat(p["created_at"]).timestamp() for h, p in by_acc.items()}
        burst, start = _tightest(list(firsts.values()), window_h * 3600)
        if burst < min_accounts:
            continue
        in_burst = [h for h, t in firsts.items() if start <= t <= start + window_h * 3600]
        ages = [account_age_days(by_acc[h]) for h in by_acc]
        known = [a for a in ages if a is not None]
        small = sum(1 for p in by_acc.values() if int(p.get("author_followers") or 0) < SMALL_FOLLOWERS) / len(by_acc)
        new_share = (sum(1 for a in known if a < 90) / len(known)) if known else 0.0
        lanes: dict[str, int] = defaultdict(int)
        for p in ps:
            if p.get("lane") and p["lane"] != "other":
                lanes[p["lane"]] += 1
        biggest = max(ps, key=lambda q: int(q["view_count"] or 0))
        rings.append({
            "url": url, "domain": url.split("/")[0], "kind": "post" if url.startswith("x.com/status/") else "link",
            "accounts": len(by_acc), "posts": len(ps), "views": sum(int(p["view_count"] or 0) for p in ps),
            "burst_accounts": burst, "burst_start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "burst_hours": window_h, "first": min(p["created_at"] for p in ps), "last": max(p["created_at"] for p in ps),
            "new_accounts": new_share, "small_accounts": small,
            # reporters and poll aggregators share a story within the hour and that is news, not a push;
            # a ring of new or small accounts moving one link together is the signature of paid distribution
            "suspicious": new_share >= 0.2 or small >= 0.6,
            "lanes": dict(sorted(lanes.items(), key=lambda kv: -kv[1])),
            "handles": sorted(in_burst, key=lambda h: -int(by_acc[h]["view_count"] or 0))[:12],
            "sample": re.sub(r"\s+", " ", biggest.get("text") or "")[:200], "sample_url": biggest.get("url"),
            "post_ids": [p["id"] for p in ps],
        })
    rings.sort(key=lambda r: (not r["suspicious"], -r["burst_accounts"], -r["views"]))
    return rings


def rings(inc: Incident) -> Path:
    store = Store(inc.db_path)
    posts = [p for p in store.all_posts() if int(p.get("in_dataset") or 0) == 1]
    store.close()
    found = find_rings(posts)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "slug": inc.slug, "min_accounts": MIN_ACCOUNTS, "window_hours": WINDOW_H,
           "stats": {"posts_with_links": sum(1 for p in posts if _urls(p)), "rings": len(found),
                     "suspicious": sum(1 for r in found if r["suspicious"]),
                     "ring_posts": sum(r["posts"] for r in found), "ring_views": sum(r["views"] for r in found)},
           "rings": found[:100]}
    path = inc.out_dir / "rings.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}  ({len(found)} link rings of {MIN_ACCOUNTS}+ accounts within {WINDOW_H:g}h, "
          f"{sum(1 for r in found if r['suspicious'])} from new or small accounts; "
          + "; ".join(f"{r['burst_accounts']} accts {r['domain']}" for r in found[:3]) + ")")
    return path


def ring_post_ids(inc: Incident, suspicious_only: bool = True) -> set[str]:
    """Post ids that belong to a ring (by default only rings of new or small accounts), for the control score."""
    path = inc.out_dir / "rings.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return set()
    return {pid for r in data.get("rings", []) if r.get("suspicious") or not suspicious_only for pid in r.get("post_ids", [])}
