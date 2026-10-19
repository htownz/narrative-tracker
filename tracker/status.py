"""Run health: credit balance, last run, per-tracker state — written to site/status.json and shown on the hub.

    python -m tracker.status site      (the workflow calls this before the hub)

Reads each incident's store meta (pulled_at, pull_partial, est_cost_usd, pull_progress) and, when
TWITTERAPI_IO_KEY is set, the twitterapi.io balance. Never raises: a missing key or a network error
just leaves that field null.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import incident_paths, ROOT, Incident, env, load_incident
from .store import Store

CREDITS_PER_USD = 100_000
LOW_USD = 1.00


def balance() -> dict[str, Any] | None:
    key = env("TWITTERAPI_IO_KEY")
    if not key:
        return None
    try:
        r = requests.get("https://api.twitterapi.io/oapi/my/info", headers={"X-API-Key": key}, timeout=20)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        d = r.json()
        credits = int(d.get("recharge_credits") or 0) + int(d.get("total_bonus_credits") or 0)
        return {"credits": credits, "usd": round(credits / CREDITS_PER_USD, 2), "recharge_credits": int(d.get("recharge_credits") or 0),
                "bonus_credits": int(d.get("total_bonus_credits") or 0), "low": credits < LOW_USD * CREDITS_PER_USD,
                "free_tier": int(d.get("recharge_credits") or 0) == 0}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def tracker_state(inc: Incident) -> dict[str, Any]:
    st: dict[str, Any] = {"slug": inc.slug, "title": inc.title, "live": inc.live and not inc.paused, "paused": inc.paused, "start": inc.start, "end": inc.end,
                          "lanes": len(inc.lanes), "exists": inc.db_path.exists()}
    if not inc.db_path.exists():
        return st
    store = Store(inc.db_path)
    try:
        st["posts"] = store.count("in_dataset=1")
        st["posts_1k"] = store.count(f"in_dataset=1 AND view_count >= {inc.min_views}")
        st["labelled"] = store.count("lane IS NOT NULL")
        st["unlabelled_1k"] = store.count(f"in_dataset=1 AND view_count >= {inc.min_views} AND lane IS NULL")
        st["pulled_at"] = store.get_meta("pulled_at")
        st["pull_partial"] = bool(store.get_meta("pull_partial", False))
        prog = store.get_meta("pull_progress", {}) or {}
        st["queries_done"] = sum(1 for v in prog.values() if v.get("done"))
        st["queries_total"] = len(prog)
        st["est_cost_usd"] = store.get_meta("est_cost_usd")
        st["errors"] = sum(1 for v in prog.values() if v.get("error"))
    finally:
        store.close()
    return st


def build(site_dir: Path) -> dict[str, Any]:
    trackers = []
    for y in incident_paths():
        try:
            trackers.append(tracker_state(load_incident(y)))
        except Exception as e:  # noqa: BLE001
            trackers.append({"slug": y.stem, "error": str(e)[:120]})
    try:
        from .usage import summary
        claude = summary(days=7, run_id=env("GITHUB_RUN_ID"))
    except Exception as e:  # noqa: BLE001
        claude = {"error": str(e)[:120]}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "balance": balance(), "trackers": trackers, "claude": claude,
           "run_url": (f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{env('GITHUB_REPOSITORY')}/actions/runs/{env('GITHUB_RUN_ID')}"
                       if env("GITHUB_RUN_ID") and env("GITHUB_REPOSITORY") else None)}
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "status.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    b = out["balance"]
    print(f"status: {len(trackers)} trackers; balance " + (f"${b['usd']:.2f}{' LOW' if b.get('low') else ''}" if b and 'usd' in b else "unknown")
          + (f"; Claude this run ~${claude['last_run']['est_usd']:.2f} ({claude['last_run']['calls']} calls), today ~${claude['today']['est_usd']:.2f}, 7d ~${claude['recent']['est_usd']:.2f}" if claude.get("last_run") else ""))
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site")
