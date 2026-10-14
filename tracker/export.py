"""Write out/<slug>/data.json — everything the chart needs, nothing it doesn't."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .config import OTHER_KEY, PUSHBACK_KEY, Incident
from .graph import build_edges
from .store import Store


def _post_out(p: dict[str, Any], lane_keys: set[str]) -> dict[str, Any]:
    lane = p.get("lane") or OTHER_KEY
    if lane not in lane_keys:
        lane = OTHER_KEY
    return {
        "id": p["id"],
        "t": p["created_at"],
        "views": int(p.get("view_count") or 0),
        "likes": int(p.get("like_count") or 0),
        "reposts": int(p.get("retweet_count") or 0),
        "handle": p.get("author_handle") or "",
        "name": p.get("author_name") or "",
        "avatar": p.get("author_avatar") or "",
        "followers": int(p.get("author_followers") or 0),
        "text": p.get("text") or "",
        "url": p.get("url") or "",
        "lane": lane,
        "copy_origin": p.get("copy_origin"),
        "in_dataset": int(p.get("in_dataset") or 0),
    }


def export(inc: Incident, include_other: bool = False, synthetic: bool = False) -> dict[str, Any]:
    store = Store(inc.db_path)
    lanes = inc.lane_table()
    lane_keys = {l["key"] for l in lanes} | {OTHER_KEY}

    dataset = store.dataset(inc.min_views)
    chart_posts = [p for p in dataset if include_other or (p.get("lane") or OTHER_KEY) != OTHER_KEY]
    chart_ids = {p["id"] for p in chart_posts}

    # Reference-only posts (what in-window posts quoted/replied to) are drawn only if they sit inside
    # the window, clear the threshold and have a lane — otherwise they'd pull the x-axis back weeks.
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    window_start = datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()
    refs = []
    for p in store.all_posts():
        if p["id"] in chart_ids or int(p.get("in_dataset") or 0) == 1:
            continue
        lane_ok = include_other or (p.get("lane") or OTHER_KEY) != OTHER_KEY
        if lane_ok and p["created_at"] and p["created_at"] >= window_start and int(p.get("view_count") or 0) >= inc.min_views:
            refs.append(p)
    by_id = {p["id"]: p for p in chart_posts + refs}
    edges = build_edges(by_id)

    # copy-cluster sizes for the "same words xN" labels
    cluster_size: dict[str, int] = {}
    for p in by_id.values():
        o = p.get("copy_origin")
        if o and o in by_id:
            cluster_size[o] = cluster_size.get(o, 0) + 1

    posts_out = []
    for p in sorted(by_id.values(), key=lambda r: r["created_at"] or ""):
        o = _post_out(p, lane_keys)
        o["copies"] = cluster_size.get(p["id"], 0)
        posts_out.append(o)

    n_push = sum(1 for p in posts_out if p["lane"] not in (PUSHBACK_KEY, OTHER_KEY) and p["in_dataset"])
    n_repeat = sum(1 for p in posts_out if p["copy_origin"] and p["in_dataset"])
    n_back = sum(1 for p in posts_out if p["lane"] == PUSHBACK_KEY and p["in_dataset"])

    from .markets import fetch_markets
    markets = fetch_markets(inc) if inc.raw.get("markets") else []

    data = {
        "markets": markets,
        "meta": {
            "slug": inc.slug,
            "title": inc.title,
            "subject": inc.subject,
            "subtitle": inc.subtitle,
            "window": {"start": inc.start, "end": inc.end, "tz": inc.timezone},
            "min_views": inc.min_views,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pulled_at": (lambda v: (v + "+00:00") if v and not re.search(r"(Z|[+-]\d\d:\d\d)$", v) else v)(store.get_meta("pulled_at")),
            "source": "SYNTHETIC SAMPLE — not real posts" if synthetic else "Data via twitterapi.io",
            "synthetic": synthetic,
            "credit": "",
        },
        "lanes": lanes,
        "posts": posts_out,
        "edges": edges,
        "stats": {"pushing": n_push, "repeated": n_repeat, "pushback": n_back, "total": len(posts_out)},
    }
    out = inc.out_dir / "data.json"
    out.write_text(json.dumps(data, ensure_ascii=False))
    print(f"wrote {out}  ({len(posts_out)} posts, {len(edges)} edges; pushing={n_push} repeated={n_repeat} pushback={n_back})")
    store.close()
    return data
