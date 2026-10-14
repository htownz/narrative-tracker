"""Early warning: stories forming outside the lanes, and lanes that are suddenly accelerating.

    python run.py alerts incidents/x.yaml

Two detectors over the last `alerts.window_hours` (default 24):
  new_cluster — posts labelled 'other' (or not yet labelled) grouped into stories; a group that reaches
                alerts.min_posts AND alerts.min_views becomes an alert with a ready-to-paste lane suggestion.
                Grouping uses Claude when ANTHROPIC_API_KEY is set, else word-overlap clustering.
  lane_surge  — a lane whose views in the last 6 hours are >= alerts.surge_ratio x its prior 6-hour average
                (and >= alerts.min_views).
Alerts are written to out/<slug>/alerts.json, printed, appended to $GITHUB_STEP_SUMMARY when present, and sent
to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set. The same story is not re-alerted for 48 hours.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import OTHER_KEY, PUSHBACK_KEY, Incident, env
from .dedup import jaccard, normalise, shingles
from .lanes import _ask, _clean, _client
from .store import Store

DEFAULTS = {"window_hours": 24, "min_posts": 8, "min_views": 100_000, "surge_ratio": 3.0, "cluster_similarity": 0.25}

CLUSTER_SYSTEM = """You spot stories forming on X before they have a name. You will see recent posts about a subject that did
not fit any existing narrative lane. Group them into distinct emerging stories (a specific claim, event, or attack line —
not a broad topic). Ignore posts that are isolated or generic. Respond with JSON only."""

CLUSTER_USER = """Subject: {subject}
Existing lanes (do not re-create these): {lanes}

Posts (id | views | handle | text):
{posts}

Return JSON: {{"clusters": [{{"key": "short_snake_key", "label": "3-6 word lane header", "description": "One sentence: what the posts claim or argue.",
"keywords": ["4-8 lowercase phrases typical of it"], "post_ids": ["..."]}}]}}
Only include clusters with at least 3 posts. A post belongs to at most one cluster."""


def _cfg(inc: Incident) -> dict[str, Any]:
    c = dict(DEFAULTS)
    c.update(inc.raw.get("alerts") or {})
    return c


def _cluster_fallback(posts: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Word-overlap clustering (2-word shingles, Jaccard) with union-find; names from the top post."""
    sh = {p["id"]: shingles(normalise(p["text"]), 2) for p in posts}
    parent = {p["id"]: p["id"] for p in posts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    ids = list(sh)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if jaccard(sh[ids[i]], sh[ids[j]]) >= threshold:
                parent[find(ids[j])] = find(ids[i])
    groups: dict[str, list[dict]] = defaultdict(list)
    by_id = {p["id"]: p for p in posts}
    for pid in ids:
        groups[find(pid)].append(by_id[pid])
    out = []
    for members in groups.values():
        if len(members) < 3:
            continue
        top = max(members, key=lambda p: p["view_count"])
        words = [w for w in normalise(top["text"]) if len(w) > 3][:6]
        key = re.sub(r"[^a-z0-9_]+", "_", "_".join(words[:3])) or "cluster"
        out.append({"key": key, "label": " ".join(words[:5]).title(), "description": _clean(top["text"], 160),
                    "keywords": words, "post_ids": [p["id"] for p in members]})
    return out


def _cluster_claude(inc: Incident, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    listing = "\n".join(f"{p['id']} | {p['view_count']} | @{p['author_handle']} | {_clean(p['text'])}" for p in posts[:150])
    lanes = "; ".join(f"{l['key']}: {l['label']}" for l in inc.lane_table())
    res = _ask(_client(), inc.classifier.get("model", "claude-sonnet-5-5"), CLUSTER_SYSTEM,
               CLUSTER_USER.format(subject=inc.subject, lanes=lanes, posts=listing))
    out = []
    for c in res.get("clusters") or []:
        key = re.sub(r"[^a-z0-9_]+", "_", str(c.get("key", "")).lower()).strip("_") or "cluster"
        out.append({"key": key, "label": str(c.get("label", key)), "description": str(c.get("description", "")),
                    "keywords": [str(k).lower() for k in (c.get("keywords") or [])][:8], "post_ids": [str(x) for x in (c.get("post_ids") or [])]})
    return out


def detect(inc: Incident) -> list[dict[str, Any]]:
    cfg = _cfg(inc)
    store = Store(inc.db_path)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=float(cfg["window_hours"]))).isoformat()
    recent = [p for p in store.all_posts() if p["in_dataset"] and p["created_at"] and p["created_at"] >= since
              and int(p["view_count"] or 0) >= inc.min_views // 2]
    alerts: list[dict[str, Any]] = []

    # 1) stories forming outside the lanes
    unlaned = [p for p in recent if (p["lane"] or OTHER_KEY) == OTHER_KEY]
    unlaned.sort(key=lambda p: -int(p["view_count"] or 0))
    clusters: list[dict[str, Any]] = []
    if len(unlaned) >= 3:
        if inc.classifier.get("provider", "claude") == "claude" and env("ANTHROPIC_API_KEY"):
            try:
                clusters = _cluster_claude(inc, unlaned[:150])
            except BaseException as e:  # no key, API error, bad JSON — never let early warning break the run
                print(f"  claude clustering unavailable ({type(e).__name__}); using word overlap", file=sys.stderr)
                clusters = _cluster_fallback(unlaned[:300], float(cfg["cluster_similarity"]))
        else:
            clusters = _cluster_fallback(unlaned[:300], float(cfg["cluster_similarity"]))
    by_id = {p["id"]: p for p in unlaned}
    for c in clusters:
        members = [by_id[i] for i in c["post_ids"] if i in by_id]
        views = sum(int(p["view_count"] or 0) for p in members)
        if len(members) >= int(cfg["min_posts"]) and views >= int(cfg["min_views"]):
            members.sort(key=lambda p: -int(p["view_count"] or 0))
            alerts.append({
                "type": "new_cluster", "key": c["key"], "title": c["label"], "posts": len(members), "views": views,
                "first": min(p["created_at"] for p in members), "accounts": len({p["author_handle"] for p in members}),
                "samples": [{"handle": p["author_handle"], "views": int(p["view_count"] or 0), "text": _clean(p["text"], 200), "url": p["url"]} for p in members[:3]],
                "suggested_lane": {"key": c["key"], "label": c["label"], "description": c["description"], "keywords": c["keywords"]},
                "_ids": [p["id"] for p in members],
            })

    # 2) lanes accelerating
    lanes = [l for l in inc.lane_table()]
    six = (now - timedelta(hours=6)).isoformat()
    for l in lanes:
        lp = [p for p in recent if p["lane"] == l["key"]]
        last6 = sum(int(p["view_count"] or 0) for p in lp if p["created_at"] >= six)
        prior = sum(int(p["view_count"] or 0) for p in lp if p["created_at"] < six)
        prior_per6 = prior / max(1.0, (float(cfg["window_hours"]) - 6) / 6)
        if last6 >= int(cfg["min_views"]) and last6 >= float(cfg["surge_ratio"]) * max(prior_per6, 1):
            top = sorted((p for p in lp if p["created_at"] >= six), key=lambda p: -int(p["view_count"] or 0))[:3]
            alerts.append({"type": "lane_surge", "key": l["key"], "title": l["label"], "views_6h": last6,
                           "prior_6h_avg": int(prior_per6), "ratio": round(last6 / max(prior_per6, 1), 1),
                           "samples": [{"handle": p["author_handle"], "views": int(p["view_count"] or 0), "text": _clean(p["text"], 200), "url": p["url"]} for p in top]})

    # suppress repeats within 48h: lane surges by lane key, clusters by post overlap (their keys change run to run)
    cutoff = (now - timedelta(hours=48)).isoformat()
    seen: dict[str, str] = {k: v for k, v in (store.get_meta("alerted", {}) or {}).items() if v >= cutoff}
    seen_clusters = [c for c in (store.get_meta("alerted_clusters", []) or []) if c.get("t", "") >= cutoff]
    fresh = []
    for a in alerts:
        if a["type"] == "new_cluster":
            ids = set(a.pop("_ids", []))
            if any(len(ids & set(c["ids"])) >= 0.5 * max(1, len(ids)) for c in seen_clusters):
                a["repeat"] = True
            else:
                seen_clusters.append({"t": now.isoformat(), "ids": sorted(ids)})
        else:
            k = f"{a['type']}:{a['key']}"
            if k in seen:
                a["repeat"] = True
            else:
                seen[k] = now.isoformat()
        fresh.append(a)
    store.set_meta("alerted", seen)
    store.set_meta("alerted_clusters", seen_clusters)
    store.close()
    return fresh


def _markdown(inc: Incident, alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return f"### {inc.title}: nothing forming outside the lanes\n"
    lines = [f"### {inc.title}: {len(alerts)} alert(s)"]
    for a in alerts:
        rep = " (repeat)" if a.get("repeat") else ""
        if a["type"] == "new_cluster":
            lines.append(f"- **New story forming{rep}: {a['title']}** — {a['posts']} posts, {a['views']:,} views, {a['accounts']} accounts since {a['first'][:16]}")
        else:
            lines.append(f"- **Lane surging{rep}: {a['title']}** — {a['views_6h']:,} views in 6h vs {a['prior_6h_avg']:,} avg ({a['ratio']}x)")
        for s in a["samples"][:2]:
            lines.append(f"  - @{s['handle']} ({s['views']:,}): {s['text'][:140]}")
        if a["type"] == "new_cluster":
            sl = a["suggested_lane"]
            lines.append("  - suggested lane: `" + json.dumps({"key": sl["key"], "label": sl["label"], "description": sl["description"], "keywords": sl["keywords"]}, ensure_ascii=False) + "`")
    return "\n".join(lines) + "\n"


def _telegram(text: str) -> None:
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)


def alerts_cmd(inc: Incident) -> list[dict[str, Any]]:
    found = detect(inc)
    (inc.out_dir / "alerts.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "alerts": found}, ensure_ascii=False))
    md = _markdown(inc, found)
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    new = [a for a in found if not a.get("repeat")]
    if new:
        _telegram(re.sub(r"[*`#]", "", _markdown(inc, new)))
    return found
