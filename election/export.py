"""Writes out/election/*.json from the store — what the dashboard front end reads.

  latest.json    every tracked race as of the newest snapshot: candidates, votes, pct, leader, margin, reporting
  series.json    per race: one row per snapshot [fetched_at, total, precincts_pct, leader_party, margin_pct, votes-by-party]
  counties.json  newest county-level rows for the statewide races + county turnout
  status.json    store counts, last poll, last change
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import OUT_DIR
from .registry import load_registry, sort_key
from .store import Store

COUNTY_RACES = ["us-senate", "governor", "lt-governor", "attorney-general", "comptroller"]   # county rows exported in full


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 2) if d else None


def _party_votes(cands: list[dict[str, Any]]) -> dict[str, int]:
    pv: dict[str, int] = defaultdict(int)
    for c in cands:
        pv[c["party"] if c["party"] in ("R", "D") else "O"] += c["votes"]
    return dict(pv)


def _leader(cands: list[dict[str, Any]], total: int) -> dict[str, Any]:
    if not cands or not total:
        return {"leader": None, "runner_up": None, "margin_votes": 0, "margin_pct": None}
    top = sorted(cands, key=lambda c: -c["votes"])
    lead, second = top[0], (top[1] if len(top) > 1 else None)
    mv = lead["votes"] - (second["votes"] if second else 0)
    return {"leader": lead["id"], "leader_party": lead["party"], "runner_up": second["id"] if second else None,
            "margin_votes": mv, "margin_pct": _pct(mv, total)}


def _surname(n: str) -> str:
    n = re.sub(r"\s*\(.*?\)\s*", " ", str(n or "")).strip()
    toks = [t for t in re.split(r"[\s,]+", n) if t and not re.fullmatch(r"(jr|sr|ii|iii|iv)\.?", t, re.I)]
    return re.sub(r"[^a-z]", "", toks[-1].lower()) if toks else ""


def blend(store: Store, key: str, cands: list[dict[str, Any]], total: int, pr: int, pt: int) -> dict[str, Any] | None:
    """County-first totals: where a county's own report has more votes for this race than the SOS's row for that county,
    swap the county's SOS numbers for the report's. Returns {candidates, total, pr, pt, counties, votes_added} or None."""
    reports = store.latest_county_reports(key)
    if not reports:
        return None
    sos = {}
    for r in store.latest_county_votes(key):
        d = sos.setdefault(r["county"], {"votes": {}, "total": 0, "pr": r["precincts_reporting"], "pt": r["precincts_total"]})
        d["votes"][r["cand_id"]] = r["votes"]; d["total"] += r["votes"]
    rep: dict[str, dict[str, Any]] = {}
    for r in reports:
        d = rep.setdefault(r["county"], {"rows": [], "total": 0, "pr": r["precincts_reporting"], "pt": r["precincts_total"]})
        d["rows"].append(r); d["total"] += r["votes"]
    by_cid = {c["id"]: c for c in cands}
    match = {(c["party"], _surname(c["name"])): c["id"] for c in cands}
    new_votes = {c["id"]: c["votes"] for c in cands}
    used, added = [], 0
    for county, d in rep.items():
        base = sos.get(county) or {"votes": {}, "total": 0, "pr": 0, "pt": 0}
        if d["total"] <= base["total"]:
            continue
        mapped = {}
        for r in d["rows"]:
            cid = match.get((r["party"], _surname(r["name"])))
            if cid is None:
                cid = next((i for (p, sn), i in match.items() if sn == _surname(r["name"])), None)   # party code differs (W, L vs feed) but the name matches
            if cid is not None:
                mapped[cid] = mapped.get(cid, 0) + r["votes"]
        if sum(mapped.values()) <= base["total"]:
            continue
        for cid in set(list(mapped) + list(base["votes"])):
            new_votes[cid] = new_votes.get(cid, 0) - base["votes"].get(cid, 0) + mapped.get(cid, 0)
        added += sum(mapped.values()) - base["total"]
        pr += max(0, (d["pr"] or 0) - (base["pr"] or 0))
        if (d["pt"] or 0) > (base["pt"] or 0):
            pt += (d["pt"] or 0) - (base["pt"] or 0)
        used.append(county)
    if not used:
        return None
    out = [dict(c, votes=new_votes.get(c["id"], c["votes"])) for c in cands]
    return {"candidates": out, "total": sum(c["votes"] for c in out), "pr": pr, "pt": pt, "counties": used, "votes_added": added}


def export(store: Store, out_dir: Path = OUT_DIR) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    reg = load_registry()
    snaps = store.snapshots()
    last = snaps[-1] if snaps else None
    calls = store.calls()
    races_rows = store.races()

    latest_races: list[dict[str, Any]] = []
    series: dict[str, list[list[Any]]] = {}
    for rr in sorted(races_rows, key=lambda r: sort_key(r["key"])):
        key = rr["key"]
        cands_meta = {c["cand_id"]: dict(c) for c in store.candidates(key)}
        by_snap: dict[Any, dict[str, Any]] = {}
        if rr["section"] == "Local":
            for row in store.local_series(key):
                s = by_snap.setdefault(row["fetched_at"], {"fetched_at": row["fetched_at"], "source_updated": row["run_time"], "votes": {},
                                                           "early": {}, "total": 0, "pr": row["precincts_reporting"], "pt": row["precincts_total"], "cr": None, "ct": None})
                s["votes"][row["cand_id"]] = row["votes"]
                s["early"][row["cand_id"]] = row["early_votes"]
        else:
            for row in store.race_series(key):
                s = by_snap.setdefault(row["id"], {"fetched_at": row["fetched_at"], "source_updated": row["source_updated"], "votes": {},
                                                    "early": {}, "total": row["total"], "pr": row["precincts_reporting"],
                                                    "pt": row["precincts_total"], "cr": row["counties_reporting"], "ct": row["counties_total"]})
                s["votes"][row["cand_id"]] = row["votes"]
                s["early"][row["cand_id"]] = row["early_votes"]
        ser = []
        newest: dict[str, Any] | None = None
        for sid in sorted(by_snap):
            s = by_snap[sid]
            cands = [{"id": cid, "name": m["name"], "party": m["party"], "incumbent": bool(m["incumbent"]),
                      "votes": s["votes"].get(cid, 0), "early_votes": s["early"].get(cid, 0)} for cid, m in cands_meta.items()]
            total = sum(c["votes"] for c in cands) or (s["total"] or 0)
            lead = _leader(cands, total)
            ser.append([s["fetched_at"], total, _pct(s["pr"] or 0, s["pt"] or 0), lead.get("leader_party"), lead["margin_pct"], _party_votes(cands)])
            newest = {"snapshot_id": sid, **s, "cands": cands, "total": total, "lead": lead}
        series[key] = ser
        meta = json.loads(rr["meta"] or "{}")
        blended = None
        if newest and rr["section"] != "Local":
            blended = blend(store, key, newest["cands"], newest["total"], newest["pr"] or 0, newest["pt"] or 0)
            if blended:
                newest["cands"], newest["total"], newest["pr"], newest["pt"] = blended["candidates"], blended["total"], blended["pr"], blended["pt"]
                newest["lead"] = _leader(newest["cands"], newest["total"])
        if newest:
            for c in newest["cands"]:
                c["pct"] = _pct(c["votes"], newest["total"])
            newest["cands"].sort(key=lambda c: (-c["votes"], c["name"]))
        call = calls.get(key)
        latest_races.append({
            "key": key, "title": rr["title"], "chamber": rr["chamber"], "report_section": rr["report_section"],
            "region": rr["region"], "competitive": bool(rr["competitive"]), "open_seat": bool(rr["open_seat"]),
            "note": rr["note"], "sos_name": rr["sos_name"], "baseline": meta.get("baseline"),
            "source": "county" if rr["section"] == "Local" else "sos", "county": meta.get("county"),
            "no_incumbent_on_ballot": meta.get("no_incumbent_on_ballot"),
            "candidates": newest["cands"] if newest else [{"id": cid, "name": m["name"], "party": m["party"], "incumbent": bool(m["incumbent"]), "votes": 0, "early_votes": 0, "pct": None} for cid, m in cands_meta.items()],
            "total": newest["total"] if newest else 0,
            "reporting": {"precincts": [newest["pr"], newest["pt"]] if newest else [0, 0],
                          "precincts_pct": _pct(newest["pr"] or 0, newest["pt"] or 0) if newest else None,
                          "counties": [newest["cr"], newest["ct"]] if newest else [0, 0]},
            **(newest["lead"] if newest else _leader([], 0)),
            "updated": newest["source_updated"] if newest else None,
            "call": dict(call) if call else None,
            "blend": {"counties": blended["counties"], "votes_added": blended["votes_added"]} if blended else None,
        })

    as_of = {"fetched_at": last["fetched_at"] if last else None, "source_updated": last["source_updated"] if last else None,
             "version": last["version"] if last else None,
             "reporting": {"counties": [last["counties_reporting"], last["counties_total"]] if last else [0, 0],
                           "precincts": [last["precincts_reporting"], last["precincts_total"]] if last else [0, 0],
                           "polling": [last["polling_reporting"], last["polling_total"]] if last else [0, 0]}}
    latest = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "as_of": as_of,
              "snapshots": len(snaps), "races": latest_races,
              "benchmarks": reg.get("turnout_benchmarks") or {}, "focus_counties": reg.get("focus_counties") or [],
              "local_races": reg.get("local") or {}, "baselines_verified": bool(reg.get("baselines_verified"))}
    (out_dir / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "series.json").write_text(json.dumps({"generated_at": latest["generated_at"], "series": series}, separators=(",", ":")), encoding="utf-8")

    # county level
    turnout = {}
    for t in store.latest_turnout():
        turnout[t["county"]] = {"code": t["code"], "registered": t["registered"], "votes_cast": t["votes_cast"],
                                "turnout_pct": _pct(t["votes_cast"], t["registered"]), "precincts": [t["precincts_reporting"], t["precincts_total"]],
                                "polling": [t["polling_reporting"], t["polling_total"]]}
    county_races: dict[str, dict[str, Any]] = {}
    for key in COUNTY_RACES:
        cmeta = {c["cand_id"]: dict(c) for c in store.candidates(key)}
        if not cmeta:
            continue
        rows = store.latest_county_votes(key)
        per: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = per.setdefault(r["county"], {"votes": {}, "precincts": [r["precincts_reporting"], r["precincts_total"]], "total": 0, "snapshot_id": r["snapshot_id"]})
            party = cmeta.get(r["cand_id"], {}).get("party") or "O"
            party = party if party in ("R", "D") else "O"
            d["votes"][party] = d["votes"].get(party, 0) + r["votes"]
            d["votes"][f"{party}_early"] = d["votes"].get(f"{party}_early", 0) + r["early_votes"]
            d["total"] += r["votes"]
            d["snapshot_id"] = max(d["snapshot_id"], r["snapshot_id"])
        for d in per.values():
            rv, dv = d["votes"].get("R", 0), d["votes"].get("D", 0)
            d["margin_pct"] = _pct(rv - dv, d["total"]) if d["total"] else None
        county_races[key] = per
    counties = {"generated_at": latest["generated_at"], "as_of": as_of, "turnout": turnout, "races": county_races}
    (out_dir / "counties.json").write_text(json.dumps(counties, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    status = {"generated_at": latest["generated_at"], "store": str(store.path.name), "counts": store.counts(), "as_of": as_of,
              "last_poll": dict(store.db.execute("SELECT * FROM polls ORDER BY fetched_at DESC LIMIT 1").fetchone() or {}) or None,
              "local": [dict(r) for r in store.local_status()]}
    (out_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    return latest
