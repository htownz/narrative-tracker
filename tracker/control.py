"""Narrative-control score: how much a lane (or a canonical claim) looks like a push rather than organic spread.

Five features, each mapped to 0-1 against what organic spread looks like in these trackers, then weighted
into a 0-100 score. The features are always shown next to the score; the number alone means little.

  copies        share of posts that repeat an earlier post's words (same-words rings)          weight 30
  new accounts  share of posts from accounts under NEW_ACCOUNT_DAYS old when they posted         weight 25
  burst         share of posts landing inside the single busiest hour                           weight 15
  regularity    how evenly spaced the copies are (bots and paid rings post on a clock)          weight 15
  top-5 share   share of views coming from the five biggest accounts                            weight 15

control(inc) -> out/<slug>/control.json  {lanes: [...], claims: [...], flagged: [...]}; the analysis page
renders the lane table and the hub lists anything at FLAG or above as a coordinated push.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PUSHBACK_KEY, Incident
from .store import Store, parse_created_at

NEW_ACCOUNT_DAYS = 90
FLAG = 70
WEIGHTS = {"copies": 25, "new_accounts": 20, "burst": 15, "regularity": 15, "top5": 10, "link_rings": 15}
# raw value -> 0..1: (floor, ceiling); below the floor is organic, above the ceiling is as bad as it gets
SCALE = {"copies": (0.02, 0.40), "new_accounts": (0.03, 0.25), "burst": (0.10, 0.60), "regularity": (0.0, 1.0), "top5": (0.45, 0.95),
         "link_rings": (0.02, 0.30)}
LABELS = {"copies": "copies", "new_accounts": "new accounts", "burst": "burst", "regularity": "regular copy timing", "top5": "top-5 share",
          "link_rings": "link rings"}

def _window_start_utc(inc) -> str:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()



def _norm(key: str, value: float) -> float:
    lo, hi = SCALE[key]
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def account_age_days(post: dict[str, Any]) -> float | None:
    """Days between the author's account creation and the post, from the raw API object."""
    try:
        raw = json.loads(post.get("raw") or "{}")
    except (TypeError, ValueError):
        return None
    created = parse_created_at((raw.get("author") or {}).get("createdAt"))
    if not created or not post.get("created_at"):
        return None
    return (datetime.fromisoformat(post["created_at"]) - datetime.fromisoformat(created)).total_seconds() / 86400


def features(posts: list[dict[str, Any]], origin_handle: dict[str, str] | None = None, ring_ids: set[str] | None = None) -> dict[str, Any]:
    """Raw feature values for a set of posts (one lane, one claim, one ring). With origin_handle (post id ->
    author of the post it copies), an account repeating itself does not count as a copy: that is spam, not a ring.
    ring_ids: posts that sit in a link ring of new or small accounts (tracker/rings.py)."""
    n = len(posts)
    if n == 0:
        return {"posts": 0}
    in_ring = sum(1 for p in posts if ring_ids and p["id"] in ring_ids)
    views = sum(int(p["view_count"] or 0) for p in posts)
    copies = [p for p in posts if p.get("copy_origin")
              and (origin_handle is None or origin_handle.get(p["copy_origin"], "") != p["author_handle"])]
    ages = [account_age_days(p) for p in posts]
    known = [a for a in ages if a is not None]
    new = sum(1 for a in known if a < NEW_ACCOUNT_DAYS)
    hours: dict[str, int] = defaultdict(int)
    for p in posts:
        hours[p["created_at"][:13]] += 1
    by_acc: dict[str, int] = defaultdict(int)
    for p in posts:
        by_acc[p["author_handle"]] += int(p["view_count"] or 0)
    top5 = sum(sorted(by_acc.values(), reverse=True)[:5]) / views if views else 0.0
    # regularity: 1 - coefficient of variation of the gaps between consecutive copies in the biggest ring
    rings: dict[str, list[float]] = defaultdict(list)
    for p in copies:
        rings[p["copy_origin"]].append(datetime.fromisoformat(p["created_at"]).timestamp())
    regularity, ring_size = 0.0, 0
    for ts in rings.values():
        if len(ts) >= 5 and len(ts) > ring_size:
            ts.sort()
            gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0]
            if len(gaps) >= 3 and statistics.mean(gaps) > 0:
                cv = statistics.pstdev(gaps) / statistics.mean(gaps)
                regularity, ring_size = max(0.0, 1.0 - cv), len(ts)
    return {
        "posts": n, "views": views, "accounts": len(by_acc),
        "copies": len(copies) / n, "copies_n": len(copies),
        "new_accounts": (new / len(known)) if known else 0.0, "new_accounts_n": new, "ages_known": len(known),
        "burst": max(hours.values()) / n, "busiest_hour": max(hours, key=hours.get),
        "regularity": regularity, "ring_size": ring_size,
        "top5": top5,
        "link_rings": in_ring / n, "link_rings_n": in_ring,
    }


def score(f: dict[str, Any], events: list[dict[str, Any]] | None = None) -> tuple[int, list[str]]:
    """0-100 and the features that drove it, strongest first. A burst inside the hours after a calendar
    event (incidents/events.yaml) is what an event does, so it does not count."""
    if not f.get("posts"):
        return 0, []
    parts = {k: _norm(k, float(f.get(k, 0.0))) * w for k, w in WEIGHTS.items()}
    if f.get("accounts", 0) < 10:
        parts["top5"] = 0.0          # five accounts out of six carrying the views says nothing
    follows = None
    if events and f.get("busiest_hour"):
        from .events import nearest_before
        follows = nearest_before(f["busiest_hour"] + ":00:00+00:00", events)
        if follows:
            parts["burst"] = 0.0
    total = int(round(sum(parts.values())))
    why = []
    for k, v in sorted(parts.items(), key=lambda kv: -kv[1]):
        if v < 4:
            continue
        raw = f[k]
        if k == "copies":
            why.append(f"{raw:.0%} of posts are copies ({f['copies_n']})")
        elif k == "new_accounts":
            why.append(f"{raw:.0%} from accounts under {NEW_ACCOUNT_DAYS} days old ({f['new_accounts_n']})")
        elif k == "burst":
            why.append(f"{raw:.0%} of posts in one hour")
        elif k == "regularity":
            why.append(f"copies spaced on a clock (ring of {f['ring_size']})")
        elif k == "top5":
            why.append(f"top five accounts carry {raw:.0%} of views")
        elif k == "link_rings":
            why.append(f"{raw:.0%} of posts push one link with new or small accounts ({f['link_rings_n']})")
    if follows and f.get("burst", 0) >= SCALE["burst"][0]:
        why.append(f"burst follows: {follows['label']}")
    return total, why


def _row(key: str, label: str, color: str | None, posts: list[dict[str, Any]], kind: str, origin_handle: dict[str, str] | None = None,
         events: list[dict[str, Any]] | None = None, ring_ids: set[str] | None = None) -> dict[str, Any]:
    f = features(posts, origin_handle, ring_ids)
    s, why = score(f, events)
    return {"kind": kind, "key": key, "label": label, "color": color, "score": s, "why": why, **f}


def control(inc: Incident) -> Path:
    store = Store(inc.db_path)
    lanes = inc.lane_table()
    lane_keys = {l["key"] for l in lanes}
    from .events import load_events
    from .rings import ring_post_ids
    events = load_events(inc)
    ring_ids = ring_post_ids(inc)
    drawn = store.chart_posts(inc.min_views, _window_start_utc(inc))
    origin_handle = {p["id"]: p["author_handle"] for p in drawn}
    posts = [p for p in drawn if p["lane"] in lane_keys]
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in posts:
        by_lane[p["lane"]].append(p)
    rows = [_row(l["key"], l["label"], l["color"], by_lane.get(l["key"], []), "lane", origin_handle, events, ring_ids) for l in lanes if l["key"] != PUSHBACK_KEY]
    # canonical claims with enough posts to judge
    by_id = {p["id"]: p for p in posts}
    cl_posts: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in store.conn.execute("SELECT cluster_id, post_id FROM claims WHERE cluster_id IS NOT NULL"):
        if r["post_id"] in by_id:
            cl_posts[int(r["cluster_id"])][r["post_id"]] = by_id[r["post_id"]]
    cl_text = {int(c["id"]): c["text"] for c in store.clusters()}
    claim_rows = [_row(str(cid), cl_text.get(cid, f"claim {cid}"), None, list(ps.values()), "claim", origin_handle, events, ring_ids)
                  for cid, ps in cl_posts.items() if len(ps) >= 5]
    claim_rows.sort(key=lambda r: (-r["score"], -r["views"]))
    store.close()
    flagged = [r for r in rows + claim_rows if r["score"] >= FLAG and r["posts"] >= 5]
    flagged.sort(key=lambda r: -r["score"])
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "slug": inc.slug, "title": inc.title, "flag": FLAG,
           "lanes": rows, "claims": claim_rows[:50], "flagged": flagged}
    path = inc.out_dir / "control.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}  (lanes: " + ", ".join(f"{r['label'][:24]}={r['score']}" for r in rows) + f"; claims scored={len(claim_rows)}; flagged={len(flagged)})")
    return path


# ---- rendering helpers shared with the analysis page and the hub -------------------------------
def table_html(rows: list[dict[str, Any]], with_color: bool = True) -> str:
    import html
    trs = []
    for r in rows:
        name = html.escape(r["label"])
        if with_color and r.get("color"):
            name = f'<span class="lane" style="color:{r["color"]}">{name}</span>'
        r.setdefault("score", 0)
        tone = "#ff5252" if r["score"] >= FLAG else "#e08a2e" if r["score"] >= 40 else "var(--ink-2)"
        trs.append(
            f'<tr><td>{name}<div class="muted">{html.escape("; ".join(r.get("why") or [])) or "nothing stands out"}</div></td>'
            f'<td class="n"><b style="color:{tone}">{r["score"]}</b></td><td class="n">{r["posts"]}</td>'
            f'<td class="n">{r.get("copies", 0):.0%}</td><td class="n">{r.get("new_accounts", 0):.0%}</td><td class="n">{r.get("burst", 0):.0%}</td>'
            f'<td class="n">{r.get("regularity", 0):.2f}</td><td class="n">{r.get("top5", 0):.0%}</td><td class="n">{r.get("link_rings", 0):.0%}</td></tr>'
        )
    return ('<div class="wrap"><table><thead><tr><th>Narrative</th><th class="n">Control</th><th class="n">Posts</th><th class="n">Copies</th>'
            f'<th class="n">New accts</th><th class="n">Burst</th><th class="n">Regularity</th><th class="n">Top-5 share</th><th class="n">Link rings</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div>')
