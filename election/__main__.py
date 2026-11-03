#!/usr/bin/env python3
"""Election results layer CLI.

  python -m election probe                       # fetch the SOS feed, print the reporting counters, store nothing
  python -m election snapshot [--no-counties]    # fetch, store if changed, write out/election/*.json
  python -m election watch --minutes 340 --every 300   # snapshot in a loop (the election-night job)
  python -m election export                      # rewrite out/election/*.json from the store
  python -m election status                      # store counts and the last poll
  python -m election races [--competitive]       # the tracked race list with current totals
  python -m election call <race-key> <candidate-name> --by "AP" [--note ...]   # record a race call
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, OUT_DIR
from . import sos
from .export import export
from .registry import tracked_races, sort_key
from .store import Store

STORE_PATH = DATA_DIR / "election-2026.sqlite"
RAW_DIR = DATA_DIR / "raw"


def _store() -> Store:
    return Store(STORE_PATH)


def _archive_raw(doc: dict, snap: dict) -> Path:
    """Gzipped copy of the decoded feed for reprocessing (gitignored; small enough to keep every change)."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = snap["fetched_at"].replace(":", "").replace("+00:00", "Z")
    ver = (snap["version"] or "v").strip("/").replace("/", "-")
    p = RAW_DIR / f"{stamp}_{ver}.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    return p


def snapshot(counties: bool = True, quiet: bool = False, local: bool = True) -> bool:
    """One poll. Returns True when the feed had changed and a snapshot was stored."""
    doc = sos.fetch(counties=counties)
    snap = sos.normalize(doc)
    st = _store()
    try:
        keyed = tracked_races(snap)
        st.upsert_races(list(keyed.values()))
        changed = st.changed_since_last(snap)
        rep = snap["reporting"]
        note = f"counties {rep['counties'][0]}/{rep['counties'][1]} polling {rep['polling'][0]}/{rep['polling'][1]}"
        st.log_poll(snap["fetched_at"], snap["version"], snap["source_updated"], changed, note)
        if changed:
            sid = st.write_snapshot(snap, keyed)
            _archive_raw(doc, snap)
            if not quiet:
                print(f"snapshot #{sid}: feed updated {snap['source_updated_raw']} ({snap['version']}) — {note}; {len(keyed)} races, {len(snap['counties'])} counties")
        elif not quiet:
            print(f"no change: feed still {snap['source_updated_raw']} ({snap['version']}) — {note}")
        if local:
            try:   # county-run races (Harris, Tarrant, Travis, Bexar); each county is best-effort
                from . import county
                races, status = county.pull()
                n = st.write_local(snap["fetched_at"], races, status)
                if not quiet:
                    summ = ", ".join(f"{c}: " + ("pending" if s.get("pending") else (f"{s['matched']} matched" if s.get("ok") else "ERR " + s.get("error", "")[:60])) for c, s in status.items())
                    print(f"local: {n} race(s) changed — {summ}")
            except Exception as e:  # noqa: BLE001
                print(f"[local] failed: {type(e).__name__}: {e}", file=sys.stderr)
        export(st)
        return changed
    finally:
        st.close()


def watch(minutes: float, every: float, counties: bool = True) -> None:
    deadline = time.time() + minutes * 60
    n = changes = 0
    while True:
        n += 1
        try:
            if snapshot(counties=counties):
                changes += 1
        except Exception as e:  # noqa: BLE001 - keep polling; the job log shows the error
            print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] poll failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.stdout.flush()
        if time.time() + every > deadline:
            break
        time.sleep(every)
    print(f"watch done: {n} polls, {changes} changes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["probe", "snapshot", "watch", "export", "status", "races", "call"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--no-counties", action="store_true", help="skip the 4 MB county file (statewide totals only)")
    ap.add_argument("--minutes", type=float, default=340, help="watch: how long to keep polling")
    ap.add_argument("--every", type=float, default=300, help="watch: seconds between polls")
    ap.add_argument("--competitive", action="store_true", help="races: only the competitive ones")
    ap.add_argument("--by", default="manual", help="call: who called it (AP, DDHQ, NYT, SOS canvass, manual)")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    if a.command == "probe":
        doc = sos.fetch(counties=not a.no_counties)
        snap = sos.normalize(doc)
        keyed = tracked_races(snap)
        rep = snap["reporting"]
        print(f"feed version {snap['version']} updated {snap['source_updated_raw']} ({snap['source_updated']})")
        print(f"counties {rep['counties']}, precincts {rep['precincts']}, polling places {rep['polling']}")
        print(f"{len(snap['races'])} contests in the feed, {len(keyed)} tracked, {len(snap['counties'])} counties")
        for key in sorted(keyed, key=sort_key)[:12]:
            r = keyed[key]
            print(f"  {key:22s} {r['title']:45s} " + " | ".join(f"{c['name']} ({c['party']}) {c['votes']:,}" for c in r["candidates"] if c["party"] in ("R", "D")))
    elif a.command == "snapshot":
        snapshot(counties=not a.no_counties)
    elif a.command == "watch":
        watch(a.minutes, a.every, counties=not a.no_counties)
    elif a.command == "export":
        st = _store(); export(st); st.close()
        print(f"wrote {OUT_DIR}/latest.json, series.json, counties.json, status.json")
    elif a.command == "status":
        st = _store()
        print(json.dumps(st.counts(), indent=1))
        last = st.db.execute("SELECT * FROM polls ORDER BY fetched_at DESC LIMIT 1").fetchone()
        print("last poll:", dict(last) if last else None)
        s = st.last_snapshot()
        print("last snapshot:", dict(s) if s else None)
        st.close()
    elif a.command == "races":
        st = _store(); latest = export(st); st.close()
        for r in latest["races"]:
            if a.competitive and not r["competitive"]:
                continue
            flag = ("*" if r["competitive"] else " ") + ("o" if r["open_seat"] else " ")
            cands = " | ".join(f"{c['name']} ({c['party']}) {c['votes']:,}" + (f" {c['pct']}%" if c["pct"] is not None else "") for c in r["candidates"][:3])
            pr = r["reporting"]["precincts_pct"]
            print(f"{flag} {r['key']:18s} {r['title'][:40]:40s} {pr if pr is not None else '-':>6}% prec  {cands}")
    elif a.command == "call":
        if len(a.args) < 2:
            ap.error("call needs <race-key> <candidate-name>")
        key, who = a.args[0], " ".join(a.args[1:])
        st = _store()
        cands = {c["name"].upper(): c["cand_id"] for c in st.candidates(key)}
        cid = cands.get(who.upper()) or next((v for k, v in cands.items() if who.upper() in k), None)
        if not cid:
            st.close(); sys.exit(f"no candidate matching '{who}' in {key}: {list(cands)}")
        st.set_call(key, cid, a.by, datetime.now(timezone.utc).isoformat(timespec="seconds"), a.note)
        export(st); st.close()
        print(f"{key}: called for {who} by {a.by}")


if __name__ == "__main__":
    main()
