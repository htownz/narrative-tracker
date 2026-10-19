"""Insights: the correlations the data already supports, recomputed every run across every tracker.

Each block is a table with its N and a plain reading written by rule, not by a model, so it says the same thing
tomorrow with more data. Nothing here is a prediction; it is what has co-occurred so far.

  0. Scorecard: did the claims flagged as "likely to grow" grow? (tracker/scorecard.py)
  1. Who posts first vs. whether a claim reaches its tracker's top five within 24h
  2. Accounts whose early touch is followed by growth (P(top 5) when they are among a claim's first posters)
  3. Event type -> lane response (views in the 12h after vs. before)
  4. Lane -> lane lead/lag within a tracker (hourly views, best lag within ±24h)
  5. Verdicts by lane: where the unsupported and misleading claims live
  6. Pushback timing vs. a lane's eventual reach
  7. Control score vs. 24h growth
  8. News crossover: which claims reached three or more outlets within 48h, and what they had in common
  9. Markets: the strongest lane-vs-price correlation seen, and whether it clears noise

insights() -> out/insights.json + out/insights/index.html (site/insights/ with the nav)
"""
from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUT_DIR

MIN_N = 20


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def _trackers() -> list[str]:
    return sorted(d.name for d in OUT_DIR.iterdir() if d.is_dir() and (d / "data.json").exists() and not _load(d / "data.json").get("meta", {}).get("synthetic"))


def _feature_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted((DATA_DIR / "features").glob("*.csv")) if (DATA_DIR / "features").exists() else []:
        if f.name == "picks.csv":                      # the scorecard's pick log, not a feature table
            continue
        with f.open(encoding="utf-8", newline="") as fh:
            rows += [r for r in csv.DictReader(fh)]
    return rows


MIN_FIELD = 10      # a top-five outcome only means something when at least this many claims competed that day


def _labelled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claim rows with a 24h outcome, on tracker-days where at least MIN_FIELD claims competed for the top five."""
    field: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        if r["kind"] == "claim":
            field[(r["slug"], r["asof"])] += 1
    return [r for r in rows if r["kind"] == "claim" and r["gain_24h"] not in ("", None) and field[(r["slug"], r["asof"])] >= MIN_FIELD]


def _titles() -> dict[str, str]:
    return {slug: _load(OUT_DIR / slug / "data.json").get("meta", {}).get("title", slug) for slug in _trackers()}


def _rate(xs: list[int]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 8 or len(a) != len(b):
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return round(num / den, 2) if den else None


# ---- 1 + 2 + 7: from the feature table ----------------------------------------------------------
def first_poster_size(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labelled = _labelled(rows)
    edges = [(0, 10_000, "under 10K followers"), (10_000, 100_000, "10K–100K"), (100_000, 1_000_000, "100K–1M"), (1_000_000, 10**12, "1M+")]
    table = []
    for lo, hi, label in edges:
        xs = [int(r["top5_24h"] == "1") for r in labelled if lo <= float(r["first_followers"] or 0) < hi]
        table.append({"bucket": label, "n": len(xs), "p_top5": _rate(xs)})
    base = _rate([int(r["top5_24h"] == "1") for r in labelled])
    big = next((t for t in table if t["bucket"] == "1M+"), None)
    small = next((t for t in table if t["bucket"] == "under 10K followers"), None)
    if len(labelled) < MIN_N:
        reading = f"Only {len(labelled)} claim-days with an outcome so far; nothing to read yet."
    elif big and small and big["p_top5"] is not None and small["p_top5"] is not None:
        reading = (f"A claim first posted by an account with a million-plus followers reached its tracker's top five within 24 hours "
                   f"{big['p_top5']:.0%} of the time, against {small['p_top5']:.0%} when the first poster had under 10K (base rate {base:.0%}, N={len(labelled)}). "
                   + ("The first poster's reach is the strongest single signal so far, and it is still a one-in-seven chance." if big["p_top5"] >= 1.5 * small["p_top5"] else "The difference is small so far."))
    else:
        reading = f"Base rate of reaching the top five within 24h: {base:.0%} over {len(labelled)} claim-days."
    return {"n": len(labelled), "base_rate": base, "table": table, "reading": reading}


def early_touch(rows: list[dict[str, Any]], claims_by_tracker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """P(top five within 24h) when a given account is among a claim's first three posters."""
    labelled = _labelled(rows)
    if len(labelled) < MIN_N:
        return {"n": len(labelled), "table": [], "reading": "Not enough outcomes yet."}
    base = _rate([int(r["top5_24h"] == "1") for r in labelled])
    touched: dict[str, list[int]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for r in labelled:
        cl = claims_by_tracker.get(r["slug"], {}).get(r["key"])
        if not cl:
            continue
        key = (r["slug"], r["key"])
        if key in seen:                       # one outcome per claim (its first labelled day)
            continue
        seen.add(key)
        firsts = cl.get("first_handles") or []
        for h in firsts[:3]:
            touched[h].append(int(r["top5_24h"] == "1"))
    table = [{"handle": h, "n": len(v), "p_top5": _rate(v)} for h, v in touched.items() if len(v) >= 5]
    table.sort(key=lambda t: (-(t["p_top5"] or 0), -t["n"]))
    top = [t for t in table if t["p_top5"] and base and t["p_top5"] >= 2 * base][:12]
    reading = (f"Accounts among a claim's first three posters whose claims reached the top five at least twice the base rate ({base:.0%}): "
               + ", ".join(f"@{t['handle']} ({t['p_top5']:.0%}, N={t['n']})" for t in top[:6]) + "." if top
               else f"No account among a claim's first three posters beats twice the base rate yet (base {base:.0%}).")
    return {"n": len(seen), "base_rate": base, "table": table[:25], "reading": reading}


def control_vs_growth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labelled = _labelled(rows)
    table = []
    for lo, hi, label in [(0, 10, "0–9"), (10, 25, "10–24"), (25, 50, "25–49"), (50, 101, "50+")]:
        g = [r for r in labelled if lo <= float(r["control"] or 0) < hi]
        if not g:
            continue
        table.append({"control": label, "n": len(g), "p_top5": _rate([int(r["top5_24h"] == "1") for r in g]),
                      "median_gain": int(statistics.median(float(r["gain_24h"]) for r in g)),
                      "p_any_gain": _rate([int(float(r["gain_24h"]) > 0) for r in g])})
    if len(labelled) < MIN_N:
        reading = "Not enough outcomes yet."
    else:
        hi = [t for t in table if t["control"] in ("25–49", "50+")]
        lo = [t for t in table if t["control"] == "0–9"]
        if hi and lo and hi[-1]["p_top5"] is not None and lo[0]["p_top5"] is not None:
            n_hi = sum(t["n"] for t in hi)
            reading = (f"Claims scoring 25+ on control (N={n_hi}) reached the top five {hi[-1]['p_top5']:.0%} of the time against {lo[0]['p_top5']:.0%} for scores under 10 (N={lo[0]['n']}). "
                       + ("Pushed claims have grown a little more often, but the pushed group is small; treat it as a lean, not a rule." if hi[-1]["p_top5"] > lo[0]["p_top5"] and n_hi < 150
                          else "Pushed claims do grow more." if hi[-1]["p_top5"] > lo[0]["p_top5"]
                          else "A high control score has not meant more growth; coordination is not the same as reach."))
        else:
            reading = "Too few high-control claims to compare."
    return {"n": len(labelled), "table": table, "reading": reading}


# ---- 3: events ----------------------------------------------------------------------------------
def event_response(trackers: list[str]) -> dict[str, Any]:
    by_type: dict[str, list[float]] = defaultdict(list)
    top_lanes: dict[str, list[str]] = defaultdict(list)
    for slug in trackers:
        a = _load(OUT_DIR / slug / "analysis.json")
        lanes = {l["key"]: l["label"] for l in _load(OUT_DIR / slug / "data.json").get("lanes", [])}
        for e in a.get("events", []):
            r = e.get("ratio")
            if isinstance(r, (int, float)) and r != float("inf"):
                by_type[e["type"]].append(float(r))
                if e.get("top_lane"):
                    top_lanes[e["type"]].append(lanes.get(e["top_lane"], e["top_lane"]))
    table = []
    for t, v in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        common = max(set(top_lanes[t]), key=top_lanes[t].count) if top_lanes[t] else ""
        table.append({"type": t, "n": len(v), "median_lift": round(statistics.median(v), 2),
                      "share_doubling": _rate([int(x >= 2) for x in v]), "lane_most_often": common})
    if not table:
        reading = "No calendar events with data after them yet."
    else:
        best = max(table, key=lambda t: t["median_lift"])
        reading = (f"{best['type'].capitalize()} events move the conversation most: median {best['median_lift']}× the views of the 12 hours before "
                   f"(N={best['n']}), landing most often in “{best['lane_most_often']}”. "
                   + "; ".join(f"{t['type']} {t['median_lift']}× (N={t['n']})" for t in table if t is not best) + ".")
    return {"table": table, "reading": reading}


# ---- 4: lane lead/lag ----------------------------------------------------------------------------
def lane_lead_lag(trackers: list[str], max_lag: int = 12) -> dict[str, Any]:
    out = []
    titles = _titles()
    for slug in trackers:
        d = _load(OUT_DIR / slug / "data.json")
        posts, lanes = d.get("posts", []), [l for l in d.get("lanes", []) if l["key"] != "pushback"]
        if len(posts) < 200:
            continue
        hours: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for p in posts:
            h = int(datetime.fromisoformat(p["t"]).timestamp() // 3600)
            hours[p["lane"]][h] += p["views"]
        all_h = sorted({h for v in hours.values() for h in v})
        if len(all_h) < 72:
            continue
        rng = list(range(all_h[0], all_h[-1] + 1))
        series = {l["key"]: [math.log1p(hours[l["key"]].get(h, 0.0)) for h in rng] for l in lanes if sum(hours[l["key"]].values()) > 0}
        keys = list(series)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                best = (0.0, 0)
                for lag in range(-max_lag, max_lag + 1):
                    if lag >= 0:
                        x, y = series[a][:len(rng) - lag], series[b][lag:]
                    else:
                        x, y = series[a][-lag:], series[b][:len(rng) + lag]
                    r = _pearson(x, y)
                    if r is not None and abs(r) > abs(best[0]):
                        best = (r, lag)
                if abs(best[0]) >= 0.3:
                    label = {l["key"]: l["label"] for l in lanes}
                    lead, follow = (a, b) if best[1] >= 0 else (b, a)
                    out.append({"tracker": titles.get(slug, slug), "leads": label[lead], "follows": label[follow], "lag_h": abs(best[1]), "r": best[0], "hours": len(rng),
                                "relation": "trades off with" if best[0] < 0 else "leads" if best[1] else "moves with"})
    out.sort(key=lambda r: -abs(r["r"]))
    leads = [r for r in out if r["relation"] == "leads"]
    together = [r for r in out if r["relation"] == "moves with"]
    tradeoff = [r for r in out if r["relation"] == "trades off with"]
    reading = ((f"Lanes that lead another lane (hourly views, best lag within ±{max_lag}h): " + "; ".join(f"{r['leads']} → {r['follows']} by {r['lag_h']}h (r={r['r']}, {r['tracker']})" for r in leads[:4]) + ". "
                if leads else f"No lane leads another by a usable margin (|r| ≥ 0.3 at a lag of 1–{max_lag}h) yet. ")
               + (f"{len(together)} pairs rise and fall in the same hour, which is one story landing in two lanes, not one lane feeding another. " if together else "")
               + (f"{len(tradeoff)} pairs trade off: one rises as the other falls." if tradeoff else ""))
    return {"table": out[:20], "reading": reading}


# ---- 5: verdicts by lane --------------------------------------------------------------------------
def verdicts_by_lane(trackers: list[str], claims_by_tracker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_lane: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    titles = _titles()
    for slug in trackers:
        for c in claims_by_tracker.get(slug, {}).values():
            if not c.get("verdict"):
                continue
            lane = (c.get("lanes") or [{}])[0].get("label", "—")
            by_lane[f"{lane} ({titles.get(slug, slug)})"][c["verdict"]] += 1
            by_type[c["type"]][c["verdict"]] += 1
    rows = []
    for lane, vs in by_lane.items():
        n = sum(vs.values())
        if n >= 3:
            bad = vs.get("unsupported", 0) + vs.get("misleading", 0)
            rows.append({"lane": lane, "n": n, "unsupported": vs.get("unsupported", 0), "misleading": vs.get("misleading", 0),
                         "supported": vs.get("supported", 0), "share_false": round(bad / n, 2)})
    rows.sort(key=lambda r: (-r["share_false"], -r["n"]))
    total = sum(sum(v.values()) for v in by_lane.values())
    if total < MIN_N:
        reading = f"{total} verdicts so far; the lane pattern needs more."
    else:
        worst = rows[0] if rows else None
        allbad = sum(r["unsupported"] + r["misleading"] for r in rows)
        reading = (f"{allbad} of {total} verified claims were unsupported or misleading. "
                   + (f"The lane with the worst record is “{worst['lane']}”: {worst['share_false']:.0%} of {worst['n']} verified claims." if worst else ""))
    return {"n": total, "table": rows[:20], "reading": reading}


# ---- 6: pushback timing --------------------------------------------------------------------------
def pushback_vs_reach(trackers: list[str]) -> dict[str, Any]:
    rows = []
    titles = _titles()
    for slug in trackers:
        for l in _load(OUT_DIR / slug / "analysis.json").get("lanes", []):
            rows.append({"tracker": titles.get(slug, slug), "lane": l["label"], "views": l["views"], "pushback_lag_h": l.get("pushback_lag_h"),
                         "pushback_share": l.get("pushback_share"), "half_life_h": l.get("half_life_h")})
    with_lag = [r for r in rows if r["pushback_lag_h"] is not None and r["views"]]
    r = _pearson([math.log10(x["views"]) for x in with_lag], [float(x["pushback_lag_h"]) for x in with_lag]) if len(with_lag) >= 8 else None
    early = [x for x in with_lag if x["pushback_lag_h"] <= 6]
    late = [x for x in with_lag if x["pushback_lag_h"] > 6]
    if early and late:
        me, ml = statistics.median(x["views"] for x in early) / 1e6, statistics.median(x["views"] for x in late) / 1e6
        reading = (f"Across {len(with_lag)} lanes with any pushback, the lanes answered within 6 hours ran a median {me:.1f}M views; lanes answered later, {ml:.1f}M. "
                   + ("Fast pushback goes with the big stories, not the small ones: a story that is already big draws an answer within hours, and the answer has not kept it small." if me >= 2 * ml
                      else "Late pushback goes with the bigger stories." if ml >= 2 * me else "How fast pushback came has not gone with how big the lane got.")
                   + f" (lag vs log views r={r}.)")
    else:
        reading = f"{len(with_lag)} lanes with pushback timing so far."
    rows.sort(key=lambda x: -x["views"])
    return {"n": len(with_lag), "r": r, "table": rows[:20], "reading": reading}


# ---- 8: news crossover ---------------------------------------------------------------------------
def news_crossover(trackers: list[str], claims_by_tracker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    checked = [c for slug in trackers for c in claims_by_tracker.get(slug, {}).values() if c.get("news") and c["news"].get("sources_48h") is not None]
    if not checked:
        return {"n": 0, "table": [], "reading": "No news checks yet."}
    crossed = [c for c in checked if c["news"]["sources_48h"] >= 3]
    press_first = [c for c in checked if c["news"]["news_before_x"]]
    by_verdict: dict[str, list[int]] = defaultdict(list)
    for c in checked:
        by_verdict[c.get("verdict") or "unverified"].append(int(c["news"]["sources_48h"] >= 3))
    table = [{"verdict": v, "n": len(xs), "p_crossover": _rate(xs)} for v, xs in sorted(by_verdict.items(), key=lambda kv: -len(kv[1]))]
    top = sorted(crossed, key=lambda c: -c["news"]["sources_48h"])[:8]
    reading = (f"{len(crossed)} of {len(checked)} checked claims reached three or more outlets within 48 hours; the press had {len(press_first)} of them before X did. "
               + (f"Biggest crossover: “{top[0]['text'][:90]}” ({top[0]['news']['sources_48h']} outlets)." if top else ""))
    return {"n": len(checked), "crossed": len(crossed), "press_first": len(press_first), "table": table,
            "top": [{"claim": c["text"], "outlets_48h": c["news"]["sources_48h"], "verdict": c.get("verdict"), "views": c["views"]} for c in top], "reading": reading}


# ---- 10: platforms --------------------------------------------------------------------------------
def platforms(trackers: list[str], claims_by_tracker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Where canonical claims start and whether they cross platforms (X, Bluesky, YouTube)."""
    from .store import SOURCES
    allc = [c for slug in trackers for c in claims_by_tracker.get(slug, {}).values() if c.get("sources")]
    multi = [c for c in allc if len(c["sources"]) > 1]
    seen_any = {src for c in allc for src in c["sources"]}
    if len(seen_any) < 2:
        return {"n": len(allc), "table": [], "reading": "Only one platform has data so far; the Bluesky pull fills this in from its first run."}
    first: dict[str, int] = defaultdict(int)
    lags: dict[str, list[float]] = defaultdict(list)
    for c in multi:
        first[c["first_source"]] += 1
        if c.get("platform_lag_h") is not None:
            lags[c["first_source"]].append(float(c["platform_lag_h"]))
    table = [{"platform": SOURCES.get(src, src), "first_on_cross_claims": n, "share": round(n / len(multi), 2),
              "median_lag_h": round(statistics.median(lags[src]), 1) if lags[src] else None} for src, n in sorted(first.items(), key=lambda kv: -kv[1])]
    single = [c for c in allc if len(c["sources"]) == 1 and c["first_source"] == "x"]
    mv_multi = statistics.median(c["views"] for c in multi) if multi else 0
    mv_single = statistics.median(c["views"] for c in single) if single else 0
    top = table[0] if table else None
    reading = (f"{len(multi)} of {len(allc)} canonical claims appeared on more than one platform. "
               + (f"{top['platform']} had it first {top['share']:.0%} of the time, with a median {top['median_lag_h']}h before the next platform. " if top and top["median_lag_h"] is not None else "")
               + (f"Claims that crossed platforms ran a median {mv_multi/1e6:.1f}M views on X against {mv_single/1e3:.0f}K for X-only claims." if multi and single else ""))
    return {"n": len(allc), "cross": len(multi), "table": table, "reading": reading}


# ---- 9: markets ----------------------------------------------------------------------------------
def mistruth_block() -> dict[str, Any]:
    """The mistruth-at-first-sight model's last report (tracker/mistruth.py writes out/mistruth/), read, not retrained."""
    m = _load(OUT_DIR / "mistruth" / "model.json")
    q = _load(OUT_DIR / "mistruth" / "queue.json")
    if not m:
        return {"reading": "No model yet: python run.py mistruth trains it from the verified claims.", "models": [], "calibration": [], "importance": [], "queue": []}
    if not m.get("models"):
        return {"reading": m.get("status") or "Not enough verified claims yet.", "models": [], "calibration": [], "importance": [], "queue": []}
    chosen = m.get("chosen")
    models = [{"model": k + (" (kept)" if k == chosen else ""), "auc": v["auc"], "brier": v["brier"], "precision_top_fifth": v["precision_top_fifth"]} for k, v in m["models"].items()]
    cal = m["models"][chosen]["calibration"] if chosen else []
    thr = (q or {}).get("threshold")
    queue = [{"tracker": x["slug"], "claim": x["text"], "views": x["views"], "first": "@" + x["first_handle"], "score": x["score"]}
             for x in (q or {}).get("queue", []) if thr is None or x["score"] >= thr][:12]
    return {"reading": m["reading"], "models": models, "calibration": cal, "importance": m.get("importance", []), "queue": queue,
            "n_labelled": m.get("n_labelled"), "base_rate": m.get("base_rate"), "threshold": thr, "trained_at": m.get("trained_at")}


def markets(trackers: list[str]) -> dict[str, Any]:
    rows = []
    titles = _titles()
    for slug in trackers:
        a = _load(OUT_DIR / slug / "analysis.json")
        for r in a.get("lead_lag", []):
            rows.append({"tracker": titles.get(slug, slug), "market": a.get("market"), "lane": r["label"], "lag_h": r["lag_h"], "r": r["r"]})
    rows.sort(key=lambda r: -abs(r["r"]))
    best = rows[0] if rows else None
    reading = (f"The strongest lane-to-price relation seen is {best['lane']} vs {best['market']} at r={best['r']:+.2f} ({best['lag_h']:+d}h). "
               + ("That clears the noise floor; worth watching." if abs(best["r"]) >= 0.4 else "Nothing above |r|=0.4: narrative volume on X has not led the markets so far.")
               if best else "No market data yet.")
    return {"table": rows[:12], "reading": reading}


def insights() -> Path:
    trackers = _trackers()
    rows = _feature_rows()
    claims_by_tracker: dict[str, dict[str, Any]] = {}
    for slug in trackers:
        cj = _load(OUT_DIR / slug / "claims.json")
        by_id = {}
        for c in cj.get("clusters", []):
            # the first three posters, from the spread series' order (claims.json keeps first_handle only) — fall back to it
            c["first_handles"] = c.get("first_handles") or [c.get("first_handle")]
            by_id[str(c["id"])] = c
        claims_by_tracker[slug] = by_id
    from .scorecard import scorecard
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "trackers": trackers, "feature_rows": len(rows),
        "scorecard": scorecard(rows),
        "first_poster": first_poster_size(rows), "early_touch": early_touch(rows, claims_by_tracker), "control": control_vs_growth(rows),
        "events": event_response(trackers), "lead_lag": lane_lead_lag(trackers), "verdicts": verdicts_by_lane(trackers, claims_by_tracker),
        "pushback": pushback_vs_reach(trackers), "news": news_crossover(trackers, claims_by_tracker), "markets": markets(trackers),
        "platforms": platforms(trackers, claims_by_tracker), "mistruth": mistruth_block(),
    }
    (OUT_DIR / "insights.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    page = render(out)
    print(f"wrote {page}  ({len(trackers)} trackers, {len(rows)} feature rows, {out['first_poster']['n']} claim-days with outcomes)")
    return page


CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  * { box-sizing:border-box; } body { margin:0 auto; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; padding:16px 16px 48px; max-width:1000px; }
  h1 { font-size:20px; margin:0 0 4px; } .lead { color:var(--ink-3); font-size:12px; margin:0 0 18px; max-width:90ch; } h2 { font-size:14px; margin:28px 0 6px; color:var(--accent); }
  .read { font-size:14px; line-height:1.5; max-width:90ch; margin:0 0 10px; } .wrap { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:12px; }
  th { text-align:left; color:var(--ink-3); font-weight:600; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; } td.n { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; } .muted { color:var(--ink-3); font-size:11px; }
  footer { margin-top:22px; color:var(--ink-3); font-size:11px; }
"""


def _table(cols: list[tuple[str, str, bool]], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted">No rows yet.</p>'
    head = "".join(f'<th class="{"n" if num else ""}">{html.escape(h)}</th>' for h, _, num in cols)
    def cell(r, key, num):
        v = r.get(key)
        if v is None:
            return "—"
        if isinstance(v, float) and (key.startswith(("p_", "share")) or key == "hit_rate"):
            return f"{v:.0%}"
        if isinstance(v, (int, float)) and key == "lift":
            return f"{v}×"
        if isinstance(v, (int, float)) and key in ("views", "median_gain", "views_6h", "gain_24h"):
            return f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K" if v >= 1e3 else str(int(v))
        return html.escape(str(v))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if num else ""}">{cell(r, key, num)}</td>' for _, key, num in cols) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(out: dict[str, Any]) -> Path:
    e = html.escape
    sc = out["scorecard"]
    sc_rows = [{"set": "All picks", **sc["all"]}, {"set": "Ranks 1–3", **sc["top3"]}, {"set": "Ranks 4–10", **sc["rank4_10"]}, {"set": "Shown on the hub", **sc["shown"]}]
    sc_latest = [{"tracker": r["slug"], "rank": r["rank"], "claim": r["label"], "views_6h": r["views_6h"], "gain_24h": r["gain_24h"], "result": "top five" if r["hit"] else "—"} for r in sc["latest"]]
    secs = [
        ("0. Scorecard: did the flagged claims grow?", sc["reading"],
         _table([("Picks", "set", False), ("Scored", "n", True), ("Reached top 5", "hits", True), ("Hit rate", "hit_rate", True), ("Lift vs base", "lift", True)], sc_rows)
         + (f'<p class="muted">Base rate {sc["base_rate"]:.0%} over {sc["field_n"]:,} claim-days on competitive snapshots; {sc["n_logged"]} of {sc["n_scored"]} picks were logged live, the rest backtested. Latest scored snapshot: {html.escape(str(sc["latest_asof"] or "")[:16])}.</p>' if sc["base_rate"] is not None else "")
         + _table([("Tracker", "tracker", False), ("Rank", "rank", True), ("Claim", "claim", False), ("Views in 6h", "views_6h", True), ("Gain 24h", "gain_24h", True), ("Result", "result", False)], sc_latest)),
        ("1. Who posts first", out["first_poster"]["reading"], _table([("First poster", "bucket", False), ("Claim-days", "n", True), ("Reached top 5 in 24h", "p_top5", True)], out["first_poster"]["table"])),
        ("2. Accounts whose early touch is followed by growth", out["early_touch"]["reading"], _table([("Account", "handle", False), ("Claims touched early", "n", True), ("Reached top 5", "p_top5", True)], out["early_touch"]["table"])),
        ("3. Events and the lanes that answer", out["events"]["reading"], _table([("Event type", "type", False), ("Events", "n", True), ("Median lift (12h after / before)", "median_lift", True), ("Share doubling", "share_doubling", True), ("Lane most often", "lane_most_often", False)], out["events"]["table"])),
        ("4. Lanes that lead other lanes", out["lead_lag"]["reading"], _table([("Tracker", "tracker", False), ("Lane", "leads", False), ("Relation", "relation", False), ("Lane", "follows", False), ("Lag h", "lag_h", True), ("r", "r", True), ("Hours", "hours", True)], out["lead_lag"]["table"])),
        ("5. Where the false claims live", out["verdicts"]["reading"], _table([("Lane", "lane", False), ("Verified", "n", True), ("Unsupported", "unsupported", True), ("Misleading", "misleading", True), ("Supported", "supported", True), ("Share false", "share_false", True)], out["verdicts"]["table"])),
        ("6. Pushback timing and reach", out["pushback"]["reading"], _table([("Tracker", "tracker", False), ("Lane", "lane", False), ("Views", "views", True), ("Pushback lag h", "pushback_lag_h", True), ("Pushback share", "pushback_share", True), ("Half-life h", "half_life_h", True)], out["pushback"]["table"])),
        ("7. Control score and growth", out["control"]["reading"], _table([("Control", "control", False), ("Claim-days", "n", True), ("Reached top 5", "p_top5", True), ("Any growth", "p_any_gain", True), ("Median 24h gain", "median_gain", True)], out["control"]["table"])),
        ("8. Crossing into the news", out["news"]["reading"], _table([("Verdict", "verdict", False), ("Checked", "n", True), ("3+ outlets in 48h", "p_crossover", True)], out["news"]["table"]) + _table([("Claim", "claim", False), ("Outlets in 48h", "outlets_48h", True), ("By lean", "sides", False), ("Verdict", "verdict", False), ("Views", "views", True)], out["news"].get("top", []))),
        ("9. Where claims start: X, Bluesky, YouTube", out["platforms"]["reading"], _table([("Platform", "platform", False), ("First on cross-platform claims", "first_on_cross_claims", True), ("Share", "share", True), ("Median lead h", "median_lag_h", True)], out["platforms"]["table"])),
        ("10. Markets", out["markets"]["reading"], _table([("Tracker", "tracker", False), ("Market", "market", False), ("Lane", "lane", False), ("Best lag h", "lag_h", True), ("r", "r", True)], out["markets"]["table"])),
        ("11. Mistruth at first sight: can a claim be flagged before anyone checks it?", out["mistruth"]["reading"],
         _table([("Learner", "model", False), ("AUC", "auc", True), ("Brier", "brier", True), ("Top fifth false", "precision_top_fifth", True)], out["mistruth"]["models"])
         + (_table([("Score bin (held out)", "bin", True), ("Claims", "n", True), ("Mean score", "mean_score", True), ("Actually false", "false_share", True)], out["mistruth"]["calibration"]) if out["mistruth"]["calibration"] else "")
         + (_table([("Feature", "feature", False), ("Weight", "weight", True)], out["mistruth"]["importance"]) if out["mistruth"]["importance"] else "")
         + (f'<p class="muted">Features are only what is visible when the claim first appears: wording (numbers, quotes, attribution, absolutes), stance, the first poster\'s size, platform and whether they are an official, '
            f'whether it followed a calendar event, and the first poster\'s and lane\'s share of false claims among claims verified before this one appeared. Validation is time-ordered (train on the earliest 70%, test on the latest 30%). '
            f'Trained {html.escape(str(out["mistruth"].get("trained_at") or "")[:16])} on {out["mistruth"].get("n_labelled")} verified claims, base rate {(out["mistruth"].get("base_rate") or 0):.0%} false.</p>' if out["mistruth"]["models"] else "")
         + (("<h3 style=\"font-size:13px;margin:14px 0 4px\">Verify first</h3><p class=\"muted\">The highest-scored unverified claims across trackers (20,000+ views). A queue for checking, not a judgment; the verify step takes half its slots from here.</p>"
             + _table([("Tracker", "tracker", False), ("Claim", "claim", False), ("Views", "views", True), ("First poster", "first", False), ("Score", "score", True)], out["mistruth"]["queue"])) if out["mistruth"]["queue"] else "")),
    ]
    body = "".join(f"<h2>{e(t)}</h2><p class=\"read\">{e(r)}</p>{tbl}" for t, r, tbl in secs)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insights — Texas Narrative Trackers</title><style>{CSS}</style></head><body>
<h1>Insights: what has gone together so far</h1>
<p class="lead">Recomputed every run across {len(out["trackers"])} trackers from {out["feature_rows"]:,} feature rows and the claims, verdicts, events and markets behind the charts.
Every block states its N; readings are written by rule from the numbers, not by a model. These are co-occurrences, not causes, and the N is small until November.</p>
{body}
<footer>Generated {e(out["generated_at"][:16].replace("T", " "))} UTC.</footer></body></html>"""
    (OUT_DIR / "insights").mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "insights" / "index.html"
    path.write_text(doc, encoding="utf-8")
    return path
