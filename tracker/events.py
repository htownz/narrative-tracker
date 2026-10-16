"""Events calendar: things that happen off X (incidents/events.yaml, plus poll releases found automatically in
the claims by tracker/pollevents.py) and how the lanes respond to them.

load_events(inc)        events that bear on this tracker, with UTC timestamps (hand-entered first, then automatic
                        poll events not already covered by a hand-entered one)
response(posts, lanes, events, hours=12)
                        per event, per lane: views and posts in the `hours` after vs. the `hours` before
nearest_before(ts, events, hours=6)
                        the event an appearance follows, if one is within `hours` before it
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .config import ROOT, Incident

EVENTS_PATH = ROOT / "incidents" / "events.yaml"
AFTER_H = 12       # response window
FOLLOW_H = 6       # "first seen within N hours after an event"


def _parse_at(value: Any, tz: ZoneInfo) -> tuple[datetime, bool]:
    """'YYYY-MM-DD HH:MM' -> that moment; 'YYYY-MM-DD' -> local midnight, all-day."""
    s = str(value).strip()
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(tzinfo=tz).astimezone(timezone.utc), True
    return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=tz).astimezone(timezone.utc), False


def load_events(inc: Incident, path: Path = EVENTS_PATH, with_auto: bool = True) -> list[dict[str, Any]]:
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    tz = ZoneInfo(raw.get("timezone") or inc.timezone)
    out = []
    for e in raw.get("events") or []:
        tr = e.get("trackers", "all")
        if tr != "all" and inc.slug not in (tr or []):
            continue
        try:
            at, all_day = _parse_at(e["at"], tz)
        except (KeyError, ValueError):
            continue
        out.append({"label": str(e.get("label") or "")[:120], "type": str(e.get("type") or "other"), "at": at.isoformat(),
                    "all_day": all_day, "source": str(e.get("source") or ""), "auto": False})
    if with_auto:
        from .pollevents import auto_events, covered_by
        for e in auto_events():
            if inc.slug in e["trackers"] and not covered_by(e, out):
                out.append({k: e[k] for k in ("label", "type", "at", "all_day", "source", "auto", "detail")})
    out.sort(key=lambda e: e["at"])
    return out


def window(e: dict[str, Any], hours: int = AFTER_H) -> tuple[datetime, datetime, datetime]:
    """(before_start, at, after_end). An all-day event's 'after' is the day itself and 'before' the day before."""
    at = datetime.fromisoformat(e["at"])
    if e.get("all_day"):
        return at - timedelta(days=1), at, at + timedelta(days=1)
    return at - timedelta(hours=hours), at, at + timedelta(hours=hours)


def response(posts: list[dict[str, Any]], lanes: list[dict[str, Any]], events: list[dict[str, Any]],
             hours: int = AFTER_H, data_end: datetime | None = None) -> list[dict[str, Any]]:
    """posts: the chart's data.json rows (t = ISO time, lane, views). One row per event with enough data after it."""
    rows = []
    for e in events:
        b0, at, a1 = window(e, hours)
        if data_end is not None and a1 > data_end + timedelta(hours=1):
            continue                                # not enough data after it yet
        per = {}
        tot_b = tot_a = 0
        times = {p["id"]: datetime.fromisoformat(p["t"]) for p in posts if p.get("t")}
        for l in lanes:
            vb = va = nb = na = 0
            for p in posts:
                if p["lane"] != l["key"] or p["id"] not in times:
                    continue
                t = times[p["id"]]
                if b0 <= t < at:
                    vb += p["views"]; nb += 1
                elif at <= t < a1:
                    va += p["views"]; na += 1
            per[l["key"]] = {"before_views": vb, "after_views": va, "before_posts": nb, "after_posts": na,
                             "ratio": (va / vb) if vb else (None if va == 0 else float("inf"))}
            tot_b += vb; tot_a += va
        rows.append({**e, "lanes": per, "before_views": tot_b, "after_views": tot_a,
                     "ratio": (tot_a / tot_b) if tot_b else (None if tot_a == 0 else float("inf")),
                     "top_lane": max(per, key=lambda k: per[k]["after_views"]) if per and tot_a else None})
    return rows


def nearest_before(ts_iso: str, events: list[dict[str, Any]], hours: int = FOLLOW_H) -> dict[str, Any] | None:
    """The latest event whose moment lies within `hours` before ts (all-day events: anywhere inside that day)."""
    t = datetime.fromisoformat(ts_iso)
    best = None
    for e in events:
        at = datetime.fromisoformat(e["at"])
        ok = (at <= t < at + timedelta(days=1)) if e.get("all_day") else (at <= t <= at + timedelta(hours=hours))
        if ok and (best is None or at > datetime.fromisoformat(best["at"])):
            best = e
    return best
