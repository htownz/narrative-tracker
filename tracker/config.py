"""Incident config loading and shared paths."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
WEB_DIR = ROOT / "web"

# Validated dark-surface categorical palette (fixed order, never cycled).
LANE_COLORS = ["#3987e5", "#d95926", "#c98500", "#d55181", "#9085e9", "#e66767", "#008300"]
PUSHBACK_COLOR = "#199e70"
PUSHBACK_KEY = "pushback"
OTHER_KEY = "other"


@dataclass
class Incident:
    slug: str
    title: str
    subject: str
    subtitle: str
    start: str
    end: str
    timezone: str
    queries: list[str]
    min_faves_hint: int
    exclude_retweets: bool
    query_type: str
    min_views: int
    fetch_referenced: bool
    lanes: list[dict[str, Any]]
    pushback: dict[str, Any]
    classifier: dict[str, Any]
    dedup: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def live(self) -> bool:
        return bool(self.raw.get("live"))

    @property
    def refresh_days(self) -> int:
        """Live mode: the most recent N days are re-pulled each run (new posts + fresher view counts)."""
        return int(self.raw.get("refresh_days", 2))

    @property
    def discover_hint(self) -> str:
        return str(self.raw.get("discover_hint") or "")

    @property
    def db_path(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR / f"{self.slug}.sqlite"

    @property
    def out_dir(self) -> Path:
        d = OUT_DIR / self.slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def lane_table(self) -> list[dict[str, Any]]:
        """Lanes in display order with colours, pushback last."""
        table = []
        for i, lane in enumerate(self.lanes):
            table.append({
                "key": lane["key"],
                "label": lane["label"],
                "description": lane.get("description", ""),
                "keywords": lane.get("keywords", []),
                "color": LANE_COLORS[i % len(LANE_COLORS)],
                "order": i,
            })
        table.append({
            "key": PUSHBACK_KEY,
            "label": self.pushback.get("label", "Pushback"),
            "description": self.pushback.get("description", ""),
            "keywords": self.pushback.get("keywords", []),
            "color": PUSHBACK_COLOR,
            "order": len(self.lanes),
        })
        return table


def _resolve_window(w: dict[str, Any], tz: str) -> tuple[str, str, bool]:
    """window.end may be 'today' (live, growing window); window.rolling_days makes start = end - N days.
    Returns (start, end, live)."""
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo
    end_raw = str(w.get("end", "")).strip().lower()
    live = end_raw in ("today", "now", "live")
    today = datetime.now(ZoneInfo(tz)).date()
    end = today if live else date.fromisoformat(end_raw)
    if w.get("rolling_days"):
        start = end - timedelta(days=int(w["rolling_days"]) - 1)
    else:
        start = date.fromisoformat(str(w.get("start")))
    return start.isoformat(), end.isoformat(), live


def load_incident(path: str | Path) -> Incident:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    w = raw.get("window", {})
    tz = raw.get("timezone", "UTC")
    start, end, live = _resolve_window(w, tz)
    from datetime import date
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    title = str(raw.get("title", raw["slug"])).replace("{days}", str(days))
    subtitle = (raw.get("subtitle") or "").strip().replace("{days}", str(days))
    fmt = lambda d: date.fromisoformat(d).strftime("%b %-d").replace(" 0", " ")  # noqa: E731
    try:
        subtitle = subtitle.replace("{start}", fmt(start)).replace("{end}", fmt(end))
    except ValueError:  # Windows strftime has no %-d
        subtitle = subtitle.replace("{start}", date.fromisoformat(start).strftime("%b %d").replace(" 0", " ")) \
                           .replace("{end}", date.fromisoformat(end).strftime("%b %d").replace(" 0", " "))
    raw.setdefault("live", live)
    return Incident(
        slug=raw["slug"],
        title=title,
        subject=raw.get("subject", ""),
        subtitle=subtitle,
        start=start,
        end=end,
        timezone=tz,
        queries=list(raw.get("queries", [])),
        min_faves_hint=int(raw.get("min_faves_hint", 0)),
        exclude_retweets=bool(raw.get("exclude_retweets", True)),
        query_type=raw.get("query_type", "Latest"),
        min_views=int(raw.get("min_views", 1000)),
        fetch_referenced=bool(raw.get("fetch_referenced", True)),
        lanes=list(raw.get("lanes") or []),
        pushback=dict(raw.get("pushback") or {}),
        classifier=dict(raw.get("classifier") or {}),
        dedup=dict(raw.get("dedup") or {}),
        raw=raw,
        path=p,
    )


def incident_paths() -> list[Path]:
    """Every incident YAML in incidents/ — files with a `slug` (events.yaml and other shared files are not incidents)."""
    out = []
    for y in sorted((ROOT / "incidents").glob("*.yaml")):
        try:
            raw = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(raw, dict) and raw.get("slug"):
            out.append(y)
    return out


def save_lanes(incident: Incident, lanes: list[dict[str, Any]]) -> None:
    """Write a discovered lane list back into the incident YAML (keeps everything else)."""
    assert incident.path is not None
    text = incident.path.read_text(encoding="utf-8")
    block = yaml.safe_dump({"lanes": lanes}, sort_keys=False, allow_unicode=True, width=100).rstrip() + "\n"
    import re
    if re.search(r"^lanes:\s*\[\s*\]\s*$", text, flags=re.M):
        # keep comments and formatting: swap only the empty lanes line
        text = re.sub(r"^lanes:\s*\[\s*\]\s*$", lambda m: block.rstrip(), text, count=1, flags=re.M)
        incident.path.write_text(text, encoding="utf-8")
    else:
        raw = yaml.safe_load(text)
        raw["lanes"] = lanes
        incident.path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    incident.lanes = lanes


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    # .env in repo root, no dependency on python-dotenv
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    return default
