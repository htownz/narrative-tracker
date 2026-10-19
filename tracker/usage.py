"""Claude usage accounting: every model call the tracker makes, logged with its tokens, so the hub can say what
a run and a day cost instead of guessing.

Every call goes through lanes._ask (classify, discover, extract, cluster, alerts, brief) or verify.ask, and both
hand the reply here. record() appends one row per call to data/usage.csv (committed by the job like the feature
tables): when, which GitHub run, tracker, step, model, input and output tokens, cache tokens, and web searches.
summary() totals it by day and by step with an estimate at list price; status.py puts the last run's and the
day's figures on the hub's health strip.

Prices are list prices per million tokens as hard-coded in PRICES and are an estimate only — the Anthropic
console (console.anthropic.com, Usage and Cost) is the bill. Update PRICES when the models or prices change.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR

COLUMNS = ["at", "run_id", "slug", "step", "model", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "web_searches", "est_usd"]
# $ per million tokens: (input, output, cache write, cache read); web search per 1,000 requests. List prices — verify on the console.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-5-5": (1.0, 5.0, 1.25, 0.10),
    "claude-opus-5-5": (15.0, 75.0, 18.75, 1.50),
}
DEFAULT_PRICE = (3.0, 15.0, 3.75, 0.30)
WEB_SEARCH_PER_1000 = 10.0


def path() -> Path:
    return DATA_DIR / "usage.csv"


def estimate(model: str, inp: int, out: int, cache_w: int = 0, cache_r: int = 0, searches: int = 0) -> float:
    p = PRICES.get(model) or next((v for k, v in PRICES.items() if model.startswith(k)), DEFAULT_PRICE)
    return (inp * p[0] + out * p[1] + cache_w * p[2] + cache_r * p[3]) / 1e6 + searches * WEB_SEARCH_PER_1000 / 1000


def record(step: str, model: str, msg: Any, slug: str = "") -> dict[str, Any] | None:
    """Log one reply's usage. Tolerates replies without a usage block (tests, stubs) by logging nothing."""
    u = getattr(msg, "usage", None)
    if u is None:
        return None
    inp, out = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    cache_w, cache_r = int(getattr(u, "cache_creation_input_tokens", 0) or 0), int(getattr(u, "cache_read_input_tokens", 0) or 0)
    stu = getattr(u, "server_tool_use", None)
    searches = int(getattr(stu, "web_search_requests", 0) or 0) if stu is not None else 0
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "slug": slug, "step": step, "model": model,
           "input_tokens": inp, "output_tokens": out, "cache_write_tokens": cache_w, "cache_read_tokens": cache_r, "web_searches": searches,
           "est_usd": round(estimate(model, inp, out, cache_w, cache_r, searches), 5)}
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError:
        pass
    return row


def rows() -> list[dict[str, Any]]:
    p = path()
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _tot(rs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"calls": len(rs), "input_tokens": sum(int(r["input_tokens"]) for r in rs), "output_tokens": sum(int(r["output_tokens"]) for r in rs),
            "web_searches": sum(int(r["web_searches"] or 0) for r in rs), "est_usd": round(sum(float(r["est_usd"]) for r in rs), 2)}


def summary(days: int = 7, run_id: str | None = None) -> dict[str, Any]:
    """Totals for the last `days` days by day and by step, the last run (run_id, or the newest in the log), and all time."""
    rs = rows()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    recent = [r for r in rs if r["at"] >= since]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_step: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recent:
        by_day[r["at"][:10]].append(r); by_step[r["step"]].append(r)
    run_id = run_id or (rs[-1]["run_id"] if rs else None)
    last_run = [r for r in rs if r["run_id"] == run_id] if run_id else []
    today = [r for r in rs if r["at"][:10] == now.strftime("%Y-%m-%d")]
    return {"days": days, "since": since[:10], "all_time": _tot(rs), "recent": _tot(recent), "today": _tot(today),
            "last_run": {"run_id": run_id, **_tot(last_run)},
            "by_day": [{"day": d, **_tot(v)} for d, v in sorted(by_day.items())],
            "by_step": sorted(({"step": s, **_tot(v)} for s, v in by_step.items()), key=lambda d: -d["est_usd"]),
            "first_at": rs[0]["at"] if rs else None, "price_note": "estimate at list price (tracker/usage.py PRICES); the Anthropic console is the bill"}


if __name__ == "__main__":
    import json
    s = summary()
    print(json.dumps({k: s[k] for k in ("all_time", "recent", "today", "last_run")}, indent=1))
    for d in s["by_day"]:
        print(f"  {d['day']}  {d['calls']:4d} calls  {d['input_tokens']:>10,} in  {d['output_tokens']:>9,} out  {d['web_searches']:4d} searches  ${d['est_usd']:.2f}")
    for d in s["by_step"]:
        print(f"  {d['step']:<10} {d['calls']:4d} calls  ${d['est_usd']:.2f}")
