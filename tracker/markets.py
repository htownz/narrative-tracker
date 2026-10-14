"""Prediction-market overlay: Kalshi hourly prices for the incident's markets, plus lane-velocity vs price lead/lag.

Incident YAML:
    markets:
      - label: "Abbott wins (Kalshi)"
        series: GOVPARTYTX
        ticker: GOVPARTYTX-26-R

Kalshi's public election API needs no key. Prices are stored in out/<slug>/markets.json and inlined into
data.json by `export`, so the chart draws a price strip above the lanes.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .config import PUSHBACK_KEY, Incident

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def fetch_candles(series: str, ticker: str, start_ts: int, end_ts: int, minutes: int = 60) -> list[dict[str, Any]]:
    r = requests.get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
                     params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": minutes}, timeout=60)
    r.raise_for_status()
    out = []
    for c in r.json().get("candlesticks", []):
        p = c.get("price") or {}
        close = p.get("close_dollars") or p.get("previous_dollars")
        if close is None:
            bid, ask = (c.get("yes_bid") or {}).get("close_dollars"), (c.get("yes_ask") or {}).get("close_dollars")
            if bid and ask:
                close = (float(bid) + float(ask)) / 2
        if close is None:
            continue
        out.append({"t": int(c["end_period_ts"]) * 1000, "p": round(float(close) * 100, 1),
                    "v": float(c.get("volume_fp") or 0)})
    return out


def fetch_markets(inc: Incident) -> list[dict[str, Any]]:
    cfg = inc.raw.get("markets") or []
    if not cfg:
        return []
    from zoneinfo import ZoneInfo
    start = int(datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).timestamp())
    end = int(time.time())
    out = []
    for m in cfg:
        try:
            pts = fetch_candles(m["series"], m["ticker"], start, end)
        except Exception as e:  # keep the chart alive when Kalshi is down
            print(f"  market {m.get('ticker')}: fetch failed ({e})")
            pts = []
        out.append({"label": m.get("label", m["ticker"]), "ticker": m["ticker"], "series": m["series"],
                    "source": "Kalshi", "points": pts, "last": pts[-1]["p"] if pts else None})
        print(f"  market {m['ticker']}: {len(pts)} hourly points, last {pts[-1]['p'] if pts else '—'}¢")
    (inc.out_dir / "markets.json").write_text(json.dumps(out))
    return out


# ------------------------------------------------------------------ lead / lag
def _hourly_views(posts: list[dict[str, Any]], lane: str | None, t0: int, t1: int) -> list[float]:
    n = max(1, (t1 - t0) // 3600_000 + 1)
    v = [0.0] * n
    for p in posts:
        if lane is not None and p["lane"] != lane:
            continue
        i = (int(datetime.fromisoformat(p["t"]).timestamp() * 1000) - t0) // 3600_000
        if 0 <= i < n:
            v[i] += float(p["views"])
    return v


def _hourly_price(points: list[dict[str, Any]], t0: int, t1: int) -> list[float | None]:
    n = max(1, (t1 - t0) // 3600_000 + 1)
    v: list[float | None] = [None] * n
    for q in points:
        i = (q["t"] - t0) // 3600_000
        if 0 <= i < n:
            v[i] = q["p"]
    last = None
    for i in range(n):  # forward fill
        if v[i] is None:
            v[i] = last
        else:
            last = v[i]
    return v


def _corr(a: list[float], b: list[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 12:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def lead_lag(posts: list[dict[str, Any]], lanes: list[dict[str, Any]], market: dict[str, Any],
             max_lag_h: int = 24) -> list[dict[str, Any]]:
    """For each lane: correlation between hourly lane views and the market's hourly price CHANGE,
    at lags -max..+max hours (positive lag = narrative leads price). Returns the best lag per lane."""
    pts = market.get("points") or []
    if len(pts) < 24 or not posts:
        return []
    t0 = min(int(datetime.fromisoformat(p["t"]).timestamp() * 1000) for p in posts)
    t1 = max(max(int(datetime.fromisoformat(p["t"]).timestamp() * 1000) for p in posts), pts[-1]["t"])
    price = _hourly_price(pts, t0, t1)
    dprice = [None if (price[i] is None or price[i - 1] is None) else price[i] - price[i - 1] for i in range(len(price))]
    dprice[0] = None
    rows = []
    for lane in lanes:
        views = _hourly_views(posts, lane["key"], t0, t1)
        views_l = [math.log1p(v) for v in views]
        best = None
        for lag in range(-max_lag_h, max_lag_h + 1):
            if lag >= 0:
                a, b = views_l[: len(views_l) - lag] if lag else views_l, dprice[lag:]
            else:
                a, b = views_l[-lag:], dprice[: len(dprice) + lag]
            c = _corr(a, b)
            if c is not None and (best is None or abs(c) > abs(best["r"])):
                best = {"lag_h": lag, "r": round(c, 3)}
        if best:
            rows.append({"lane": lane["key"], "label": lane["label"], "lag_h": best["lag_h"], "r": best["r"],
                         "views": int(sum(views)), "direction": "narrative leads price" if best["lag_h"] > 0 else "price leads narrative" if best["lag_h"] < 0 else "same hour"})
    rows.sort(key=lambda r: -abs(r["r"]))
    return rows
