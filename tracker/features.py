"""Prediction feature table: one row per canonical claim and per lane, every run, appended to
data/features/<slug>.csv, with the outcome columns filled in retroactively on later runs.

Features (as of the latest post in the store, `asof`):
  age_h, posts, views, accounts, copies, first_views, first_followers,
  views_1h / views_6h / views_24h (views from posts in the last N hours), momentum_6h (last 6h vs prior 6h),
  control (0-100), new_accounts, burst, event_follow (first seen within 6h after a calendar event), event_type,
  market_price, market_change_24h
Outcomes (filled once the store reaches asof + 24h / 72h):
  gain_24h, gain_72h   views from posts that appeared in the 24 / 72 hours after asof
  top5_24h             whether the claim ranked in the tracker's top five by gain_24h
Until there is enough history to fit a model, `rank_now()` orders claims by a heuristic
(views_6h * momentum * log accounts, unexplained bursts up-weighted) and out/<slug>/features.json lists the
top ten as "likely to grow"; the hub shows the best across trackers.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, PUSHBACK_KEY, Incident
from .control import features as control_features, score as control_score
from .events import load_events, nearest_before
from .store import Store

COLUMNS = ["run_at", "asof", "slug", "kind", "key", "label", "age_h", "posts", "views", "accounts", "copies", "first_views",
           "first_followers", "views_1h", "views_6h", "views_24h", "momentum_6h", "control", "new_accounts", "burst",
           "event_follow", "event_type", "market_price", "market_change_24h", "news_sources_48h", "news_before_x", "first_reliability", "first_early_lift",
           "gain_24h", "gain_72h", "top5_24h", "measured"]

def _window_start_utc(inc) -> str:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()



def _dir() -> Path:
    d = DATA_DIR / "features"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _views_between(posts: list[dict[str, Any]], a: datetime, b: datetime) -> int:
    return sum(int(p["view_count"] or 0) for p in posts if a <= datetime.fromisoformat(p["created_at"]) < b)


def _market(inc: Incident, asof: datetime) -> tuple[float | None, float | None]:
    path = inc.out_dir / "data.json"
    if not path.exists():
        return None, None
    try:
        markets = json.loads(path.read_text(encoding="utf-8")).get("markets") or []
    except ValueError:
        return None, None
    if not markets or not markets[0].get("points"):
        return None, None
    pts = [q for q in markets[0]["points"] if q["t"] <= asof.timestamp() * 1000]
    if not pts:
        return None, None
    now = pts[-1]["p"]
    day_ago = [q for q in pts if q["t"] <= (asof - timedelta(hours=24)).timestamp() * 1000]
    return now, (now - day_ago[-1]["p"]) if day_ago else None


def _group_rows(inc: Incident, kind: str, groups: dict[str, tuple[str, list[dict[str, Any]]]], asof: datetime,
                origin_handle: dict[str, str], events: list[dict[str, Any]], market: tuple[float | None, float | None],
                run_at: str, news: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = []
    news = news or {}
    from .registry import lookup as registry_lookup
    registry = registry_lookup() if kind == "claim" else {}
    for key, (label, posts) in groups.items():
        if not posts:
            continue
        posts = sorted(posts, key=lambda p: p["created_at"])
        first = posts[0]
        reg = registry.get(first["author_handle"]) or {}
        f = control_features(posts, origin_handle)
        ctrl, _ = control_score(f, events)
        v6 = _views_between(posts, asof - timedelta(hours=6), asof + timedelta(seconds=1))
        v6p = _views_between(posts, asof - timedelta(hours=12), asof - timedelta(hours=6))
        ev = nearest_before(first["created_at"], events) if events else None
        rows.append({
            "run_at": run_at, "asof": asof.isoformat(), "slug": inc.slug, "kind": kind, "key": key, "label": label[:120],
            "age_h": round((asof - datetime.fromisoformat(first["created_at"])).total_seconds() / 3600, 1),
            "posts": len(posts), "views": f["views"], "accounts": f["accounts"], "copies": f["copies_n"],
            "first_views": int(first["view_count"] or 0), "first_followers": int(first["author_followers"] or 0),
            "views_1h": _views_between(posts, asof - timedelta(hours=1), asof + timedelta(seconds=1)),
            "views_6h": v6, "views_24h": _views_between(posts, asof - timedelta(hours=24), asof + timedelta(seconds=1)),
            "momentum_6h": round(v6 / v6p, 2) if v6p else (None if v6 == 0 else 99.0),
            "control": ctrl, "new_accounts": round(f["new_accounts"], 3), "burst": round(f["burst"], 3),
            "event_follow": int(bool(ev)), "event_type": ev["type"] if ev else "",
            "market_price": market[0], "market_change_24h": market[1],
            "news_sources_48h": (news.get(key) or {}).get("sources_48h") if kind == "claim" else None,
            "news_before_x": (news.get(key) or {}).get("news_before_x") if kind == "claim" else None,
            "first_reliability": reg.get("reliability"), "first_early_lift": reg.get("early_lift"),
            "gain_24h": None, "gain_72h": None, "top5_24h": None, "measured": None,
        })
    return rows


def rank_now(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Heuristic stand-in for the model: what is gaining right now, from more than one account,
    not explained by a calendar event. Higher is more likely to be bigger tomorrow."""
    out = []
    for r in rows:
        if r["kind"] != "claim" or r["accounts"] < 2 or not r["views_6h"]:
            continue
        mom = r["momentum_6h"] if r["momentum_6h"] is not None else 1.0
        s = math.log10(1 + r["views_6h"]) * min(5.0, max(1.0, mom)) * (1 + math.log10(r["accounts"])) * (1.3 if (r["control"] >= 40 and not r["event_follow"]) else 1.0)
        out.append({**r, "heuristic": round(s, 2)})
    out.sort(key=lambda r: -r["heuristic"])
    return out


def _views_at(series: list[tuple[str, int]], t: datetime) -> int | None:
    """The last sighting at or before t; None when the first sighting is later than t."""
    best = None
    for at, v in series:
        if datetime.fromisoformat(at) <= t:
            best = v
        else:
            break
    return best


def measured_gain(posts: list[dict[str, Any]], series: dict[str, list[tuple[str, int]]], t0: datetime, t1: datetime) -> int | None:
    """Views the group's posts gained between t0 and t1 according to view_history: for posts seen before t0 the
    difference of sightings, for posts born in between their views at t1. None when fewer than half of the
    group's posts have a sighting in the window (then the caller falls back to the reconstruction)."""
    total, covered = 0, 0
    for p in posts:
        created = datetime.fromisoformat(p["created_at"])
        if created >= t1:
            continue
        ser = series.get(p["id"]) or []
        if not any(t0 <= datetime.fromisoformat(at) <= t1 + timedelta(hours=6) for at, _ in ser):
            continue
        v1 = _views_at(ser, t1 + timedelta(hours=6))
        if v1 is None:
            continue
        v0 = _views_at(ser, t0) if created < t0 else 0
        total += max(0, v1 - (v0 or 0))
        covered += 1
    eligible = sum(1 for p in posts if datetime.fromisoformat(p["created_at"]) < t1)
    return total if eligible and covered >= max(1, eligible // 2) else None


def _label_outcomes(existing: list[dict[str, Any]], groups_by_kind: dict[str, dict[str, list[dict[str, Any]]]], asof: datetime,
                    series: dict[str, list[tuple[str, int]]] | None = None) -> int:
    """Fill gain_24h / gain_72h / top5_24h on rows old enough. Measured from view_history when it covers the
    group (measured=1), otherwise reconstructed from when posts appeared (measured=0)."""
    filled = 0
    series = series or {}
    by_asof_kind: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in existing:
        by_asof_kind[(r["asof"], r["kind"])].append(r)
    for (row_asof, kind), rows in by_asof_kind.items():
        t0 = datetime.fromisoformat(row_asof)
        for horizon, col in ((24, "gain_24h"), (72, "gain_72h")):
            if asof < t0 + timedelta(hours=horizon) or all(r[col] not in ("", None) for r in rows):
                continue
            for r in rows:
                posts = groups_by_kind.get(kind, {}).get(r["key"])
                if posts is None:
                    continue
                m = measured_gain(posts, series, t0, t0 + timedelta(hours=horizon)) if series else None
                if m is not None:
                    r[col], r["measured"] = m, 1
                else:
                    r[col] = _views_between(posts, t0, t0 + timedelta(hours=horizon))
                    r["measured"] = r.get("measured") or 0
                filled += 1
            if col == "gain_24h" and kind == "claim":
                ranked = sorted((r for r in rows if r["gain_24h"] not in ("", None)), key=lambda r: -int(r["gain_24h"]))
                top = {r["key"] for r in ranked[:5]}
                for r in rows:
                    if r["gain_24h"] not in ("", None):
                        r["top5_24h"] = int(r["key"] in top)
    return filled


def features(inc: Incident) -> Path:
    store = Store(inc.db_path)
    lanes = inc.lane_table()
    lane_keys = {l["key"] for l in lanes}
    drawn = store.chart_posts(inc.min_views, _window_start_utc(inc))
    posts = [p for p in drawn if p["lane"] in lane_keys]
    if not posts:
        store.close()
        raise SystemExit("no drawn posts yet")
    asof = max(datetime.fromisoformat(p["created_at"]) for p in posts)
    run_at = datetime.now(timezone.utc).isoformat(timespec="minutes")
    origin_handle = {p["id"]: p["author_handle"] for p in drawn}
    events = load_events(inc)
    market = _market(inc, asof)

    lane_groups = {l["key"]: (l["label"], [p for p in posts if p["lane"] == l["key"]]) for l in lanes if l["key"] != PUSHBACK_KEY}
    by_id = {p["id"]: p for p in posts}
    cl_posts: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in store.conn.execute("SELECT cluster_id, post_id FROM claims WHERE cluster_id IS NOT NULL"):
        if r["post_id"] in by_id:
            cl_posts[str(r["cluster_id"])][r["post_id"]] = by_id[r["post_id"]]
    cl_text = {str(c["id"]): c["text"] for c in store.clusters()}
    claim_groups = {cid: (cl_text.get(cid, cid), list(ps.values())) for cid, ps in cl_posts.items() if len(ps) >= 2}
    from .news import latest_checks
    news = {str(k): v for k, v in latest_checks(store).items()}
    store.close()

    new_rows = _group_rows(inc, "lane", lane_groups, asof, origin_handle, events, market, run_at) + \
        _group_rows(inc, "claim", claim_groups, asof, origin_handle, events, market, run_at, news)

    path = _dir() / f"{inc.slug}.csv"
    existing: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            existing = [dict(r) for r in csv.DictReader(f)]
    groups_by_kind = {"lane": {k: v[1] for k, v in lane_groups.items()}, "claim": {k: v[1] for k, v in claim_groups.items()}}
    st2 = Store(inc.db_path)
    series = st2.view_series({p["id"] for g in groups_by_kind.values() for ps in g.values() for p in ps})
    st2.close()
    filled = _label_outcomes(existing, groups_by_kind, asof, series)
    # one snapshot per asof: a re-run on unchanged data replaces its own rows instead of duplicating them
    existing = [r for r in existing if r["asof"] != asof.isoformat()]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in existing + new_rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in COLUMNS})
    ranked = rank_now(new_rows)[:10]
    from .scorecard import log_picks
    log_picks(inc.slug, ranked, asof.isoformat(), run_at)          # scored a day later against this table (tracker/scorecard.py)
    labelled = sum(1 for r in existing if r.get("gain_24h") not in ("", None))
    out = {"generated_at": run_at, "asof": asof.isoformat(), "slug": inc.slug, "title": inc.title,
           "rows_total": len(existing) + len(new_rows), "rows_labelled_24h": labelled,
           "likely_to_grow": [{k: r[k] for k in ("key", "label", "views", "views_6h", "momentum_6h", "accounts", "control", "event_follow", "heuristic")} for r in ranked]}
    (inc.out_dir / "features.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}  (+{len(new_rows)} rows this run, {len(existing) + len(new_rows)} total, {filled} outcomes filled, {labelled} rows with a 24h outcome)")
    for r in ranked[:5]:
        print(f"  gaining: {r['label'][:70]}  6h={r['views_6h']:,} momentum={r['momentum_6h']} accounts={r['accounts']} control={r['control']}")
    return path
