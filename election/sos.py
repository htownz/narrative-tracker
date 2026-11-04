"""Texas Secretary of State election-night returns (Civix IVIS ENR).

The public site is results.texas-election.com; it reads a JSON API whose payloads are either plain JSON or a
single `{"upload": "<base64 JSON>"}` wrapper, with the big sections (Federal, StateWide, Districted, Home, ...)
each base64-encoded again inside. Three calls cover an election:

  electionConstants              every election the system knows (ID, year Y, type ET: GE/P/RU/S/SR, name N)
  election/<id>                  statewide totals: Home (reporting counters), Federal / StateWide / Districted
                                 races, each with Candidates [ID, N, P, V votes, EV early votes, PE pct]
  election/countyInfo/<id>       254 counties: Summary (RV registered, VC/VT votes cast, PRR/PRP precincts,
                                 PLR/PLP polling places) and Races {OID: {N, C {id: {N, P, V, EV}}, PR, TP}}

`fetch()` returns the three decoded documents; `normalize()` turns them into one flat snapshot dict that the
store and exporter understand. Nothing here knows about the registry or the report.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

BASE = "https://goelect.txelections.civixapps.com/api-ivis-system/api/s3/enr/"
UA = "grey-havens-election-dashboard/0.1 (+https://github.com/htownz/narrative-tracker)"
GENERAL_2026 = 53815

PARTY = {"REP": "R", "DEM": "D", "LIB": "L", "GRN": "G", "GRE": "G", "IND": "I", "W": "W", "R": "R", "D": "D", "L": "L", "G": "G", "I": "I"}


def _decode(x: Any) -> Any:
    """Plain JSON, base64-wrapped JSON, or already a dict/list."""
    if not isinstance(x, str):
        return x
    s = x.strip()
    if s[:1] in "[{":
        return json.loads(s)
    try:
        return json.loads(base64.b64decode(s).decode("utf-8"))
    except Exception:
        return x


def _unwrap(doc: Any) -> Any:
    doc = _decode(doc)
    if isinstance(doc, dict) and set(doc) == {"upload"}:
        return _decode(doc["upload"])
    return doc


def get(path: str, timeout: int = 60, retries: int = 3) -> Any:
    url = BASE + path
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=timeout)
            r.raise_for_status()
            return _unwrap(r.json())
        except Exception as e:  # noqa: BLE001 - retry anything, report the last error
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"SOS ENR: {url} failed after {retries} tries: {last}")


def elections() -> list[dict[str, Any]]:
    c = get("electionConstants")
    out = []
    for year, by_type in (c.get("electionInfo") or {}).items():
        for et, items in by_type.items():
            for _id, e in items.items():
                out.append(e)
    return out


def general_election_id(year: str = "2026") -> int:
    for e in elections():
        if str(e.get("Y")) == year and e.get("ET") == "GE":
            return int(e["ID"])
    return GENERAL_2026


def fetch(election_id: int = GENERAL_2026, counties: bool = True) -> dict[str, Any]:
    """The decoded documents plus a UTC fetch stamp."""
    doc = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "election_id": election_id}
    doc["election"] = get(f"election/{election_id}")
    if counties:
        doc["counties"] = get(f"election/countyInfo/{election_id}")
    return doc


# ---------------------------------------------------------------------------------------------------------------
# normalisation

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}


def parse_updated(s: str | None) -> str | None:
    """'Nov 04, 2026 01:03:13' -> ISO. The SOS site shows this stamp without a zone; comparing it with the clock
    on election night (feed said 01:03 when it was 01:12 UTC, 7:12 PM Central) shows it is UTC."""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", s or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    y, d, hh, mm, ss = int(m.group(3)), int(m.group(2)), int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    return datetime(y, mon, d, hh, mm, ss, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _int(x: Any) -> int:
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def clean_name(n: str) -> str:
    """'GREG ABBOTT (I)' -> 'GREG ABBOTT'; collapses whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"\s*\(I\)\s*$", "", str(n or ""))).strip()


def is_incumbent(n: str) -> bool:
    return bool(re.search(r"\(I\)\s*$", str(n or "")))


def normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """One flat snapshot:

    {
      fetched_at, source_updated, version, election_date,
      reporting: {counties: [r, t], precincts: [r, t], polling: [r, t]},
      races: {sos_name: {sos_id, name, section, candidates: [{id, name, party, incumbent, votes, early_votes, order}],
                         total}},
      counties: {county_name: {code, registered, votes_cast, turnout_pct, precincts: [r, t], polling: [r, t],
                               races: {sos_name: {total, precincts: [r, t], votes: {cand_id: [votes, early]}}}}}
    }
    """
    e = doc["election"]
    home = _decode(e.get("Home")) or {}
    snap: dict[str, Any] = {
        "fetched_at": doc["fetched_at"],
        "election_id": doc.get("election_id"),
        "source_updated": parse_updated(home.get("LastUpdatedTime")),
        "source_updated_raw": home.get("LastUpdatedTime"),
        "version": str(e.get("Version") or ""),
        "election_date": home.get("ElecDate"),
        "refresh_minutes": _int(home.get("RefreshTime")),
        "reporting": {
            "counties": [_int((home.get("CountiesReporting") or {}).get("CR")), _int((home.get("CountiesReporting") or {}).get("CT"))],
            "precincts": [_int((home.get("PrecinctsReporting") or {}).get("PR")), _int((home.get("PrecinctsReporting") or {}).get("PT"))],
            "polling": [_int((home.get("PollingReporting") or {}).get("PLR")), _int((home.get("PollingReporting") or {}).get("PLT"))],
        },
        "races": {},
        "counties": {},
    }
    for section in ("Federal", "StateWide", "Districted"):
        sec = _decode(e.get(section)) or {}
        for r in sec.get("Races") or []:
            name = re.sub(r"\s+", " ", str(r.get("N") or "")).strip()
            cands = []
            for c in r.get("Candidates") or []:
                cands.append({
                    "id": str(c.get("ID")),
                    "name": clean_name(c.get("N")),
                    "party": PARTY.get(str(c.get("P") or "").upper(), str(c.get("P") or "")),
                    "incumbent": is_incumbent(c.get("N")),
                    "votes": _int(c.get("V")),
                    "early_votes": _int(c.get("EV")),
                    "order": _int(c.get("O")),
                })
            snap["races"][name] = {
                "sos_id": _int(r.get("id")),
                "name": name,
                "section": section,
                "candidates": cands,
                "total": sum(c["votes"] for c in cands) or _int(r.get("T")),
            }
    counties = doc.get("counties") or {}
    if isinstance(counties, dict):
        for code, c in counties.items():
            if not isinstance(c, dict) or not c.get("N"):
                continue
            s = c.get("Summary") or {}
            cname = re.sub(r"\s+", " ", str(c["N"])).strip().title()
            crec: dict[str, Any] = {
                "code": str(code),
                "registered": _int(s.get("RV")),
                "votes_cast": _int(s.get("VC")) or _int(c.get("TV")),
                "turnout_pct": float(s.get("VT") or 0) or None,
                "precincts": [_int(s.get("PRR")), _int(s.get("PRP"))],
                "polling": [_int(s.get("PLR")), _int(s.get("PLP")) or _int(s.get("NPL"))],
                "races": {},
            }
            for _oid, r in (c.get("Races") or {}).items():
                rname = re.sub(r"\s+", " ", str(r.get("N") or "")).strip()
                votes = {}
                for cid, cc in (r.get("C") or {}).items():
                    votes[str(cc.get("id") or cid)] = [_int(cc.get("V")), _int(cc.get("EV"))]
                crec["races"][rname] = {
                    "total": _int(r.get("TV")) or sum(v[0] for v in votes.values()),
                    "precincts": [_int(r.get("PR")), _int(r.get("TP"))],
                    "votes": votes,
                }
            snap["counties"][cname] = crec
    return snap


# ---------------------------------------------------------------------------------------------------------------
# race keys: the SOS contest name -> a short stable key the registry, store and dashboard share

_KEY_RULES = [
    (r"^U\.? ?S\.? SENATOR$", lambda m: "us-senate"),
    (r"^U\.? ?S\.? REPRESENTATIVE DISTRICT (\d+)$", lambda m: f"cd-{int(m.group(1))}"),
    (r"^GOVERNOR$", lambda m: "governor"),
    (r"^LIEUTENANT GOVERNOR$", lambda m: "lt-governor"),
    (r"^ATTORNEY GENERAL$", lambda m: "attorney-general"),
    (r"^COMPTROLLER OF PUBLIC ACCOUNTS$", lambda m: "comptroller"),
    (r"^COMMISSIONER OF THE GENERAL LAND OFFICE$", lambda m: "land-commissioner"),
    (r"^COMMISSIONER OF AGRICULTURE$", lambda m: "ag-commissioner"),
    (r"^RAILROAD COMMISSIONER$", lambda m: "railroad-commissioner"),
    (r"^CHIEF JUSTICE, SUPREME COURT$", lambda m: "scotx-chief"),
    (r"^JUSTICE, SUPREME COURT, PLACE (\d+)", lambda m: f"scotx-{int(m.group(1))}"),
    (r"^JUDGE, COURT OF CRIMINAL APPEALS, PLACE (\d+)", lambda m: f"cca-{int(m.group(1))}"),
    (r"^STATE SENATOR, DISTRICT (\d+)$", lambda m: f"sd-{int(m.group(1))}"),
    (r"^STATE REPRESENTATIVE DISTRICT (\d+)$", lambda m: f"hd-{int(m.group(1))}"),
    (r"^MEMBER, STATE BOARD OF EDUCATION, DISTRICT (\d+)$", lambda m: f"sboe-{int(m.group(1))}"),
]


def race_key(sos_name: str) -> str | None:
    """Short key for the contests the dashboard tracks; None for district judges, DAs, courts of appeals."""
    n = re.sub(r"\s+", " ", str(sos_name or "")).strip().upper()
    for pat, fn in _KEY_RULES:
        m = re.match(pat, n)
        if m:
            return fn(m)
    return None
