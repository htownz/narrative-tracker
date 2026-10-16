"""Scorecard: did the claims the tracker flagged as "likely to grow" actually grow?

Every run, `features()` ranks each tracker's claims with the growth heuristic and logs its top PICKS_PER_RUN to
data/features/picks.csv (rank, heuristic, whether the hub's "Gaining now" list would show it). A day later the
feature table has the outcome for those claim-days (top5_24h, gain_24h), so each pick can be scored: did it
reach its tracker's top five by 24h views? The scorecard compares the picks' hit rate with the base rate of
every claim competing that day, overall, by rank, and for the hub-shown picks only. Snapshots older than the
log are backtested by re-ranking their rows with the same heuristic (source=backtest).

This is the bar the growth model has to beat: a model that cannot out-pick a one-line heuristic is not worth
its complexity.
"""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import DATA_DIR

PICKS_PER_RUN = 10
HUB_RANK, HUB_MIN_ACCOUNTS, HUB_MIN_VIEWS_6H = 3, 3, 20_000       # what tracker/hub.py shows as "Gaining now"
MIN_FIELD = 10                                                   # a top-five is only a result when 10+ claims competed
PICK_COLUMNS = ["run_at", "asof", "slug", "key", "label", "rank", "heuristic", "views_6h", "accounts", "eligible"]


def _picks_path() -> Path:
    p = DATA_DIR / "features" / "picks.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def hub_eligible(r: dict[str, Any], rank: int) -> bool:
    return rank <= HUB_RANK and int(r["accounts"]) >= HUB_MIN_ACCOUNTS and int(r["views_6h"]) >= HUB_MIN_VIEWS_6H


def log_picks(slug: str, ranked: list[dict[str, Any]], asof: str, run_at: str) -> int:
    """Append this run's picks for one tracker, replacing any earlier rows for the same snapshot."""
    path = _picks_path()
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f) if not (r["slug"] == slug and r["asof"] == asof)]
    new = [{"run_at": run_at, "asof": asof, "slug": slug, "key": r["key"], "label": str(r["label"])[:120], "rank": i + 1,
            "heuristic": r["heuristic"], "views_6h": r["views_6h"], "accounts": r["accounts"], "eligible": int(hub_eligible(r, i + 1))}
           for i, r in enumerate(ranked[:PICKS_PER_RUN])]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PICK_COLUMNS)
        w.writeheader()
        w.writerows(rows + new)
    return len(new)


def _num(r: dict[str, Any]) -> dict[str, Any]:
    """A feature-table row with the fields the heuristic reads as numbers."""
    out = dict(r)
    out["views_6h"] = int(float(r["views_6h"] or 0))
    out["accounts"] = int(float(r["accounts"] or 0))
    out["control"] = float(r["control"] or 0)
    out["event_follow"] = int(float(r["event_follow"] or 0))
    out["momentum_6h"] = float(r["momentum_6h"]) if r["momentum_6h"] not in ("", None) else None
    return out


def scorecard(feature_rows: list[dict[str, Any]], picks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score logged picks (and backtest the heuristic on snapshots before the log) against the outcomes in the
    feature table. feature_rows: every tracker's rows; picks: picks.csv rows (read from disk when None)."""
    from .features import rank_now
    if picks is None:
        path = _picks_path()
        picks = []
        if path.exists():
            with path.open(encoding="utf-8", newline="") as f:
                picks = list(csv.DictReader(f))
    claims = [r for r in feature_rows if r["kind"] == "claim"]
    by_snap: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in claims:
        by_snap[(r["slug"], r["asof"])].append(r)
    outcome = {(r["slug"], r["asof"], r["key"]): r for r in claims if r["gain_24h"] not in ("", None)}
    logged_snaps = {(p["slug"], p["asof"]) for p in picks}
    scored: list[dict[str, Any]] = []
    for p in picks:
        o = outcome.get((p["slug"], p["asof"], p["key"]))
        if o is None or len(by_snap[(p["slug"], p["asof"])]) < MIN_FIELD:
            continue
        scored.append({**p, "rank": int(p["rank"]), "eligible": int(p["eligible"]), "hit": int(str(o["top5_24h"]) == "1"), "gain_24h": int(float(o["gain_24h"])),
                       "measured": str(o.get("measured")) == "1", "source": "logged"})
    for (slug, asof), rows in by_snap.items():
        if (slug, asof) in logged_snaps or len(rows) < MIN_FIELD or not any(r["gain_24h"] not in ("", None) for r in rows):
            continue
        for i, r in enumerate(rank_now([_num(x) for x in rows])[:PICKS_PER_RUN]):
            o = outcome.get((slug, asof, r["key"]))
            if o is None:
                continue
            scored.append({"run_at": r["run_at"], "asof": asof, "slug": slug, "key": r["key"], "label": r["label"], "rank": i + 1, "heuristic": r["heuristic"],
                           "views_6h": r["views_6h"], "accounts": r["accounts"], "eligible": int(hub_eligible(r, i + 1)),
                           "hit": int(str(o["top5_24h"]) == "1"), "gain_24h": int(float(o["gain_24h"])), "measured": str(o.get("measured")) == "1", "source": "backtest"})
    field = [r for (slug, asof), rows in by_snap.items() if len(rows) >= MIN_FIELD for r in rows if r["gain_24h"] not in ("", None)]
    base = (sum(int(str(r["top5_24h"]) == "1") for r in field) / len(field)) if field else None

    def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
        hits = sum(r["hit"] for r in rows)
        rate = hits / len(rows) if rows else None
        return {"n": len(rows), "hits": hits, "hit_rate": round(rate, 2) if rate is not None else None,
                "lift": round(rate / base, 1) if rate is not None and base else None}
    by_tracker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        by_tracker[r["slug"]].append(r)
    latest_asof = max((r["asof"] for r in scored), default=None)
    latest = sorted((r for r in scored if r["asof"] == latest_asof), key=lambda r: (r["slug"], r["rank"])) if latest_asof else []
    out = {
        "n_scored": len(scored), "n_logged": sum(1 for r in scored if r["source"] == "logged"), "base_rate": round(base, 3) if base is not None else None,
        "field_n": len(field), "snapshots": len({(r["slug"], r["asof"]) for r in scored}),
        "all": block(scored), "top3": block([r for r in scored if r["rank"] <= 3]), "rank4_10": block([r for r in scored if r["rank"] > 3]),
        "shown": block([r for r in scored if r["eligible"]]),
        "by_tracker": sorted(({"tracker": slug, **block(rows)} for slug, rows in by_tracker.items()), key=lambda t: -t["n"]),
        "latest_asof": latest_asof, "latest": [{k: r[k] for k in ("slug", "rank", "label", "views_6h", "hit", "gain_24h", "eligible")} for r in latest][:30],
        "median_gain_hits": int(statistics.median(r["gain_24h"] for r in scored if r["hit"])) if any(r["hit"] for r in scored) else None,
    }
    out["reading"] = reading(out)
    return out


def reading(s: dict[str, Any]) -> str:
    a, base = s["all"], s["base_rate"]
    if not a["n"] or base is None:
        return "No picks have an outcome yet; the first scores land a day after the first logged run."
    if a["n"] < 20:
        return f"{a['hits']} of {a['n']} picks reached the top five (base rate {base:.0%}); too few scored to read yet."
    text = (f"Of {a['n']} claims flagged as likely to grow across {s['snapshots']} snapshots, {a['hits']} reached their tracker's top five within 24 hours: "
            f"{a['hit_rate']:.0%} against a base rate of {base:.0%}, {a['lift']}× better than picking at random. ")
    t3, shown = s["top3"], s["shown"]
    if t3["n"] >= 10 and s["rank4_10"]["n"] >= 10:
        text += f"The top three picks hit {t3['hit_rate']:.0%}; ranks four to ten, {s['rank4_10']['hit_rate']:.0%}. "
    if shown["n"] >= 10:
        text += f"The picks the hub actually showed hit {shown['hit_rate']:.0%} (N={shown['n']}). "
    if a["lift"] is not None:
        verdict = ("The heuristic earns its place on the hub." if a["lift"] >= 2 else "The heuristic is better than nothing, not by much." if a["lift"] >= 1.3
                   else "The heuristic is not beating chance; the hub's flags should be read as activity, not prediction.")
        text += verdict if a["n"] >= 50 else f"Early lean, on {a['n']} scored picks: {verdict[0].lower()}{verdict[1:]} The verdict waits for 50."
    return text
