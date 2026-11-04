"""County-run races the SOS feed does not carry: county judge and commissioner races (Harris, Bexar, Tarrant)
and the Austin City Council (Travis). Each county posts its own election-night report:

  Harris   Hart Verity cumulative PDF   https://appfiles.harrisvotes.com/harrisvotes/prd/Data/1126/cumulative.pdf
  Tarrant  Hart Verity cumulative PDF   https://www.tarrantcountytx.gov/content/dam/main/elections/2026/1126/reports/cumulative.pdf
  Travis   Scytl/Clarity ENR JSON       https://results.enr.clarityelections.com/TX/Travis/  (elections.json -> EID -> current_ver.txt -> json/en/summary.json)
  Bexar    Electionware summary HTML    https://home.bexar.org/el45a.html

`pull()` fetches and parses every source, matches contests to the registry's `local:` entries by regex, and
returns normalized races: {key, county, source, contest, run_time, precincts: [r, t], candidates: [{id, name,
party, votes, early_votes, eday_votes}]}. PDFs need `pdftotext` (poppler-utils) on the PATH.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .registry import load_registry

UA = "Mozilla/5.0 (compatible; grey-havens-election-dashboard/0.1; +https://github.com/htownz/narrative-tracker)"

SOURCES: dict[str, dict[str, Any]] = {
    "Harris": {"kind": "harris_json", "url": "https://appfiles.harrisvotes.com/harrisvotes/prd/Data/1126/resultsbyballot.json"},   # the JSON behind app.harrisvotes.com; the cumulative PDF is the fallback
    "Tarrant": {"kind": "hart_pdf", "url": "https://www.tarrantcountytx.gov/content/dam/main/elections/2026/1126/reports/cumulative.pdf"},
    "Travis": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/Travis/", "date": "11/3/2026"},
    "Bexar": {"kind": "electionware_html", "url": "https://home.bexar.org/el45a.html"},
    # Nov 3, 7:30 PM: counties whose own reports run ahead of their SOS upload early in the night; their statewide,
    # congressional and legislative contests are blended into the totals (election/export.py) until the SOS catches up
    "Denton": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/Denton/", "date": "11/3/2026"},
    "Williamson": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/Williamson/", "date": "11/3/2026"},
    "El Paso": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/El_Paso/", "date": "11/3/2026"},
    "Galveston": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/Galveston/", "date": "11/3/2026"},
    "Dallas": {"kind": "clarity", "base": "https://results.enr.clarityelections.com/TX/Dallas/", "date": "11/3/2026", "eid": "127396"},   # its elections.json is empty; the EID is on dallascountyvotes.org
}

# county contest names vary ("United States Senator", "US Senator", "U.S. Senator"); map them to the registry race keys
_CK = [
    (r"^(u\.?\s?s\.?|united states) senator\b", lambda m: "us-senate"),
    (r"^(u\.?\s?s\.?|united states) representative,? (?:district )?(\d+)", lambda m: f"cd-{int(m.group(2))}"),
    (r"^governor\b", lambda m: "governor"),
    (r"^lieutenant governor\b", lambda m: "lt-governor"),
    (r"^attorney general\b", lambda m: "attorney-general"),
    (r"^comptroller of public accounts\b", lambda m: "comptroller"),
    (r"^commissioner of the general land office\b", lambda m: "land-commissioner"),
    (r"^commissioner of agriculture\b", lambda m: "ag-commissioner"),
    (r"^railroad commissioner\b", lambda m: "railroad-commissioner"),
    (r"^chief justice,? supreme court\b", lambda m: "scotx-chief"),
    (r"^justice,? supreme court,? place (\d+)", lambda m: f"scotx-{int(m.group(1))}"),
    (r"^judge,? court of criminal appeals,? place (\d+)", lambda m: f"cca-{int(m.group(1))}"),
    (r"^state senator,? district (\d+)", lambda m: f"sd-{int(m.group(1))}"),
    (r"^state representative,? district (\d+)", lambda m: f"hd-{int(m.group(1))}"),
    (r"^(?:member,? )?state board of education,? district (\d+)", lambda m: f"sboe-{int(m.group(1))}"),
]


def county_race_key(contest: str) -> str | None:
    n = re.sub(r"\s+", " ", str(contest or "")).strip().lower().replace("dist.", "district")
    n = re.sub(r"^(rep|dem)\s+", "", n)
    for pat, fn in _CK:
        m = re.match(pat, n)
        if m:
            return fn(m)
    return None

PARTY = {"REP": "R", "DEM": "D", "LIB": "L", "GRN": "G", "GRE": "G", "IND": "I", "W": "W", "": ""}


def _get(url: str, timeout: int = 60, tries: int = 3) -> requests.Response:
    """Election-night servers throw 502s and queue pages under load: retry briefly."""
    import time as _t
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            _t.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def _json(url: str) -> Any:
    r = _get(url)
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"{url}: non-JSON response ({r.headers.get('content-type', '?')}; queue or error page)")


def _n(s: Any) -> int:
    """int from '1,234', 1234, or Clarity's one-element lists like [0]."""
    if isinstance(s, (list, tuple)):
        return sum(_n(x) for x in s)
    try:
        return int(float(str(s).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def cand_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


# ---------------------------------------------------------------------------------------------------------------
# Hart Verity "Cumulative Results Report" PDF (Harris, Tarrant)

_HART_CONTEST = re.compile(r"^(?P<name>[^\n]+?)\s+-\s+Vote for (?:none or )?(?:one|\d+)\s*$", re.I)
_HART_PAIR = re.compile(r"(\d[\d,]*)\s+[\d.]+%")
_HART_PREC = re.compile(r"(\d[\d,]*)\s+of\s+(\d[\d,]*)\s*=\s*[\d.]+%")


def parse_hart_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "r.pdf"
        p.write_bytes(pdf_bytes)
        txt = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=120).stdout
    if "will be posted when it becomes" in txt:
        return {"pending": True, "contests": [], "precincts": [0, 0], "run_time": None}
    prec = [0, 0]
    m = _HART_PREC.search(txt)
    if m:
        prec = [_n(m.group(1)), _n(m.group(2))]
    run_time = None
    mt = re.search(r"Run Time\s+(\d{1,2}:\d{2} [AP]M)", txt)
    md = re.search(r"Run Date\s+(\d{2}/\d{2}/\d{4})", txt)
    if mt and md:
        try:
            run_time = datetime.strptime(f"{md.group(1)} {mt.group(1)}", "%m/%d/%Y %I:%M %p").isoformat()
        except ValueError:
            run_time = f"{md.group(1)} {mt.group(1)}"
    contests: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cols: list[str] = []
    for line in txt.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        mc = _HART_CONTEST.match(line.strip())
        if mc and not line.startswith(" " * 20):
            cur = {"contest": mc.group("name").strip(), "candidates": []}
            contests.append(cur)
            cols = []
            continue
        if cur is None:
            continue
        if line.strip().startswith("Choice"):
            cols = [c.strip() for c in re.split(r"\s{2,}", line.strip())][2:]   # after Choice, Party
            continue
        if re.search(r"Cast Votes:|Undervotes:|Overvotes:", line):
            if "Cast Votes" in line:
                pairs = _HART_PAIR.findall(line)
                if pairs:
                    cur["cast_votes"] = _n(pairs[-1])
            continue
        pairs = _HART_PAIR.findall(line)
        if len(pairs) < 2:
            continue
        head = _HART_PAIR.split(line)[0]
        parts = [x for x in re.split(r"\s{2,}", head.strip()) if x]
        if not parts:
            continue
        name = parts[0]
        party = parts[1].strip() if len(parts) > 1 and parts[1].strip().upper() in PARTY else ""
        if name.endswith("(W)") and not party:
            party = "W"
        vals = [_n(x) for x in pairs]
        total = vals[-1]
        byname = dict(zip(cols, vals)) if cols and len(cols) == len(vals) else {}
        eday = byname.get("Election Day", 0)
        early = sum(v for k, v in byname.items() if k not in ("Election Day", "Total", "ED Provisional")) if byname else 0
        cur["candidates"].append({"id": cand_id(name), "name": re.sub(r"\s*\(W\)\s*$", "", name).strip(), "party": PARTY.get(party.upper(), party),
                                  "votes": total, "early_votes": early, "eday_votes": eday})
    return {"pending": False, "contests": contests, "precincts": prec, "run_time": run_time}


# ---------------------------------------------------------------------------------------------------------------
# Harris County's results app JSON (app.harrisvotes.com -> appfiles.harrisvotes.com/.../resultsbyballot.json)

def parse_harris_json(doc: dict[str, Any]) -> dict[str, Any]:
    contests = []
    for c in doc.get("contests") or []:
        names = c.get("ca") or []
        tv, ev, ab, ed = (c.get(k) or [] for k in ("tv", "ev", "ab", "ed"))
        cands = []
        for i, raw in enumerate(names):
            m = re.match(r"^(.*?)\s*\((REP|DEM|LIB|GRN|GRE|IND|W)\)\s*$", str(raw))
            nm, party = (m.group(1).strip(), m.group(2)) if m else (str(raw).strip(), "")
            cands.append({"id": cand_id(nm), "name": nm, "party": PARTY.get(party, party), "votes": _n(tv[i] if i < len(tv) else 0),
                          "early_votes": _n(ev[i] if i < len(ev) else 0) + _n(ab[i] if i < len(ab) else 0), "eday_votes": _n(ed[i] if i < len(ed) else 0)})
        contests.append({"contest": str(c.get("t") or "").strip(), "candidates": cands, "precincts": [_n(c.get("pr")), _n(c.get("tp"))], "cast_votes": sum(x["votes"] for x in cands)})
    pr = max([c["precincts"][0] for c in contests] or [0]); tp = max([c["precincts"][1] for c in contests] or [0])
    return {"pending": not any(c["cast_votes"] for c in contests), "contests": contests, "precincts": [pr, tp], "run_time": doc.get("dt"),
            "registered": _n(doc.get("rv")), "votes_cast": _n(doc.get("vc"))}


# ---------------------------------------------------------------------------------------------------------------
# Scytl / Clarity ENR (Travis)

def clarity_election(base: str, date: str) -> dict[str, Any] | None:
    els = _json(base.rstrip("/") + "/elections.json")
    want = datetime.strptime(date, "%m/%d/%Y").date()
    for e in els:
        try:
            d = datetime.strptime(e.get("Date", "")[:10].strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        if d == want:
            return e
    return None


def parse_clarity(base: str, date: str, eid: str | None = None) -> dict[str, Any]:
    e = {"EID": eid, "ElectionName": f"Clarity election {eid}"} if eid else clarity_election(base, date)
    if not e:
        return {"pending": True, "contests": [], "precincts": [0, 0], "run_time": None, "note": f"no election dated {date} on Clarity yet"}
    eid = e["EID"]
    ver = _get(f"{base.rstrip('/')}/{eid}/current_ver.txt").text.strip()
    if not re.fullmatch(r"\d+", ver):
        return {"pending": True, "contests": [], "precincts": [0, 0], "run_time": None, "note": "Clarity returned an error page for current_ver.txt (busy)"}
    summary = _json(f"{base.rstrip('/')}/{eid}/{ver}/json/en/summary.json")
    contests = []
    tp = pr = 0
    for c in summary:
        cands = []
        for name, party, votes in zip(c.get("CH") or [], c.get("P") or [], c.get("V") or []):
            nm = re.sub(r"\s*\((REP|DEM|LIB|GRN|GRE|IND|NON|W)\)\s*$", "", str(name or "")).strip()
            cands.append({"id": cand_id(nm), "name": nm, "party": PARTY.get(str(party or "").upper(), str(party or "")),
                          "votes": _n(votes), "early_votes": 0, "eday_votes": 0})
        contests.append({"contest": str(c.get("C") or "").strip(), "candidates": cands, "precincts": [_n(c.get("PR")), _n(c.get("TP"))], "cast_votes": _n(c.get("T"))})
        tp, pr = max(tp, _n(c.get("TP"))), max(pr, _n(c.get("PR")))
    return {"pending": False, "contests": contests, "precincts": [pr, tp], "run_time": None, "election": e.get("ElectionName"), "version": ver}


# ---------------------------------------------------------------------------------------------------------------
# Electionware summary HTML (Bexar): absolutely-positioned spans; read them in document order as a token stream.

def parse_electionware_html(text: str) -> dict[str, Any]:
    spans = re.findall(r'<span style="font-family: ([^;"]+); color: [^;"]+; font-size: (\d+)px;( font-weight: bold;)?">(.*?)</span>', text, re.S)
    toks = [(fam, int(size), bool(bold), html.unescape(re.sub(r"<[^>]+>", "", t)).strip()) for fam, size, bold, t in spans]
    title = " ".join(t for _, _, _, t in toks[:6])
    if "2026" not in title and "November 3" not in title:
        return {"pending": True, "contests": [], "precincts": [0, 0], "run_time": None, "note": f"page still shows: {title[:120]}"}
    prec = [0, 0]
    m = re.search(r"Vote Center\s+(\d+)\s+of\s+(\d+)", title) or re.search(r"Precincts? Reporting\s+(\d+)\s+of\s+(\d+)", text)
    if m:
        prec = [_n(m.group(1)), _n(m.group(2))]
    contests: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cols: list[str] = []
    i = 0
    while i < len(toks):
        fam, size, bold, t = toks[i]
        if bold and size == 9 and t and not re.match(r"^(Summary Results Report|UNOFFICIAL|Election Summary)", t):
            cur = {"contest": t, "candidates": []}
            contests.append(cur)
            cols = []
            i += 1
            continue
        if cur is not None and t == "TOTAL":
            cols = ["TOTAL"]
            j = i + 1
            while j < len(toks) and toks[j][3] in ("VOTE %", "Election Day", "Absentee", "Early Voting"):
                cols.append(toks[j][3]); j += 1
            i = j
            continue
        if cur is not None and cols and t and not re.match(r"^[\d,.%]+$", t) and t not in ("Vote For 1", "Vote For 2") and not t.startswith(("Undervotes", "Overvotes", "Contest Totals", "Vote For")):
            vals = []
            j = i + 1
            while j < len(toks) and len(vals) < len(cols) and re.match(r"^[\d,.%]+$", toks[j][3]):
                vals.append(toks[j][3]); j += 1
            if len(vals) >= 2:
                name = t
                party = ""
                mp = re.match(r"^(REP|DEM|LIB|GRN|IND)\s+(.+)$", name)
                if mp:
                    party, name = mp.group(1), mp.group(2)
                byname = dict(zip(cols, vals))
                cur["candidates"].append({"id": cand_id(name), "name": name, "party": PARTY.get(party, party), "votes": _n(byname.get("TOTAL", "0")),
                                          "early_votes": _n(byname.get("Early Voting", "0")) + _n(byname.get("Absentee", "0")),
                                          "eday_votes": _n(byname.get("Election Day", "0"))})
                i = j
                continue
        i += 1
    run_time = None
    mr = re.search(r"Election Summary - (\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2} [AP]M)", text)
    if mr:
        try:
            run_time = datetime.strptime(f"{mr.group(1)} {mr.group(2)}", "%m/%d/%Y %I:%M %p").isoformat()
        except ValueError:
            pass
    return {"pending": False, "contests": contests, "precincts": prec, "run_time": run_time}


# ---------------------------------------------------------------------------------------------------------------

def fetch_county(county: str) -> dict[str, Any]:
    src = SOURCES[county]
    if src["kind"] == "harris_json":
        out = parse_harris_json(_json(src["url"] + "?nocache=" + str(int(__import__("time").time()))))
    elif src["kind"] == "hart_pdf":
        out = parse_hart_pdf(_get(src["url"]).content)
    elif src["kind"] == "clarity":
        out = parse_clarity(src["base"], src["date"], src.get("eid"))
    elif src["kind"] == "electionware_html":
        out = parse_electionware_html(_get(src["url"]).text)
    else:
        raise ValueError(src["kind"])
    out["county"] = county
    out["source"] = src.get("url") or src.get("base")
    return out


def match_local(reg: dict[str, Any], county: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Registry `local:` entries for this county whose `match` regex hits a parsed contest name."""
    out = []
    for key, spec in (reg.get("local") or {}).items():
        if spec.get("county") != county or not spec.get("match"):
            continue
        pat = re.compile(spec["match"], re.I)
        for c in parsed["contests"]:
            if pat.search(c["contest"]):
                prec = c.get("precincts") or parsed["precincts"]
                out.append({"key": key, "county": county, "source": parsed["source"], "contest": c["contest"], "title": spec.get("title") or c["contest"],
                            "run_time": parsed.get("run_time"), "precincts": prec, "cast_votes": c.get("cast_votes"),
                            "competitive": bool(spec.get("competitive")), "open_seat": bool(spec.get("open_seat")), "note": spec.get("note"),
                            "runoff_possible": bool(spec.get("runoff_possible")), "candidates": c["candidates"],
                            "digest": hashlib.md5(json.dumps([c["candidates"], prec], sort_keys=True).encode()).hexdigest()})
                break
    return out


def keyed_contests(county: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Every contest in a county report that maps to a registry race key (statewide, congressional, legislative)."""
    out = []
    for c in parsed["contests"]:
        key = county_race_key(c["contest"])
        if not key:
            continue
        prec = c.get("precincts") or parsed["precincts"]
        out.append({"key": key, "county": county, "contest": c["contest"], "precincts": prec, "run_time": parsed.get("run_time"),
                    "candidates": c["candidates"], "total": sum(x["votes"] for x in c["candidates"]),
                    "digest": hashlib.md5(json.dumps([c["candidates"], prec], sort_keys=True).encode()).hexdigest()})
    return out


def pull(counties: list[str] | None = None, reg: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """All matched local races, a per-county status dict, and every keyed contest from the county reports."""
    reg = reg or load_registry()
    races: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    for county in counties or list(SOURCES):
        try:
            parsed = fetch_county(county)
            matched = match_local(reg, county, parsed)
            races.extend(matched)
            keyed = keyed_contests(county, parsed)
            reports.extend(keyed)
            status[county] = {"ok": True, "pending": parsed.get("pending", False), "contests": len(parsed["contests"]), "matched": len(matched), "keyed": len(keyed),
                              "precincts": parsed["precincts"], "run_time": parsed.get("run_time"), "note": parsed.get("note")}
        except Exception as e:  # noqa: BLE001 - one county failing must not stop the others
            status[county] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return races, status, reports
