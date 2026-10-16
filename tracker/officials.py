"""Elected rhetoric: posts in a tracker by elected officials, candidates, agencies and public bodies
(incidents/officials.yaml), surfaced on the analysis page, the client desk, the hub and the weekly brief.

officials()              -> {handle_lower: {handle, name, office, level, party}}
is_official(handle)      -> the record or None
said(slug, days)         -> that tracker's official posts in the window, newest first, with lane labels
said_everywhere(days)    -> across every tracker, grouped by official, for the hub and the brief
table_html(rows)         -> a table for the analysis page / client desk
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import OUT_DIR, ROOT

OFFICIALS_PATH = ROOT / "incidents" / "officials.yaml"
LEVEL_ORDER = {"statewide": 0, "federal": 1, "candidate": 2, "legislature": 3, "local": 4, "agency": 5}


@lru_cache(maxsize=1)
def officials() -> dict[str, dict[str, Any]]:
    if not OFFICIALS_PATH.exists():
        return {}
    raw = yaml.safe_load(OFFICIALS_PATH.read_text(encoding="utf-8")) or {}
    out = {}
    for o in raw.get("officials") or []:
        h = str(o.get("handle") or "").strip().lstrip("@")
        if h:
            out[h.lower()] = {"handle": h, "name": o.get("name") or h, "office": o.get("office") or "", "level": o.get("level") or "other", "party": str(o.get("party") or "—")}
    return out


def is_official(handle: str | None) -> dict[str, Any] | None:
    return officials().get((handle or "").lower()) if handle else None


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def said(slug: str, days: int = 7, end: datetime | None = None) -> list[dict[str, Any]]:
    """Official posts in a tracker's chart data (X, at the threshold) in the last `days`."""
    d = _load(OUT_DIR / slug / "data.json")
    if not d:
        return []
    end = end or datetime.now(timezone.utc)
    since = (end - timedelta(days=days)).isoformat()
    lanes = {l["key"]: l for l in d.get("lanes", [])}
    rows = []
    for p in d.get("posts", []):
        o = is_official(p.get("handle"))
        if not o or p.get("t", "") < since:
            continue
        lane = lanes.get(p.get("lane"), {})
        rows.append({"slug": slug, "tracker": d.get("meta", {}).get("title", slug), "handle": o["handle"], "name": o["name"], "office": o["office"],
                     "level": o["level"], "party": o["party"], "t": p["t"], "views": int(p.get("views") or 0), "text": p.get("text") or "",
                     "url": p.get("url") or "", "lane": lane.get("label", p.get("lane") or ""), "lane_key": p.get("lane") or "", "color": lane.get("color", "#888")})
    rows.sort(key=lambda r: r["t"], reverse=True)
    return rows


def said_everywhere(days: int = 7, slugs: list[str] | None = None) -> dict[str, Any]:
    slugs = slugs or sorted(d.name for d in OUT_DIR.iterdir() if d.is_dir() and (d / "data.json").exists())
    rows = [r for s in slugs for r in said(s, days)]
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by.setdefault(r["handle"], {"handle": r["handle"], "name": r["name"], "office": r["office"], "level": r["level"], "party": r["party"], "posts": 0, "views": 0, "trackers": set(), "lanes": {}, "top": None})
        b["posts"] += 1; b["views"] += r["views"]; b["trackers"].add(r["slug"])
        b["lanes"][r["lane"]] = b["lanes"].get(r["lane"], 0) + 1
        if b["top"] is None or r["views"] > b["top"]["views"]:
            b["top"] = {"text": r["text"], "views": r["views"], "url": r["url"], "lane": r["lane"], "tracker": r["tracker"], "t": r["t"]}
    table = sorted(by.values(), key=lambda b: -b["views"])
    for b in table:
        b["trackers"] = sorted(b["trackers"])
        b["lanes"] = sorted(b["lanes"].items(), key=lambda kv: -kv[1])[:3]
    return {"days": days, "posts": rows, "by_official": table}


def _fmt(n: int | float) -> str:
    n = float(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else str(int(n))


def table_html(rows: list[dict[str, Any]], tz: str = "America/Chicago", limit: int = 40, show_tracker: bool = False) -> str:
    """Official posts as a table: who, office, when, lane, views, the post."""
    from zoneinfo import ZoneInfo
    if not rows:
        return '<p class="muted">No posts from elected officials, candidates or agencies in this window.</p>'
    z = ZoneInfo(tz)
    trs = []
    for r in rows[:limit]:
        when = datetime.fromisoformat(r["t"]).astimezone(z).strftime("%b %d, %I:%M %p").replace(" 0", " ")
        party = f' <span class="muted">({html.escape(r["party"])})</span>' if r["party"] not in ("—", "") else ""
        trs.append(f'<tr><td><b>{html.escape(r["name"])}</b>{party}<div class="muted">{html.escape(r["office"])}'
                   + (f' · {html.escape(r["tracker"])}' if show_tracker else "") + f'</div></td><td class="muted">{when}</td>'
                   f'<td><span class="lane" style="color:{r["color"]}">{html.escape(r["lane"])}</span></td><td class="n">{_fmt(r["views"])}</td>'
                   f'<td><a href="{html.escape(r["url"] or "#")}" style="color:var(--ink)">{html.escape(r["text"][:220])}</a></td></tr>')
    return ('<div class="wrap"><table><thead><tr><th>Who</th><th>When</th><th>Lane</th><th class="n">Views</th><th>Post</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')
