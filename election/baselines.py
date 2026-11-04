"""County-level baselines from the Texas Legislative Council's VTD election returns (Capitol Data Portal,
"Comprehensive Election Datasets (Compressed Format)", CC-BY). The 2024-vintage zip carries every general election
2012-2024 on 2024 VTD lines; counties are exact sums of their VTDs.

    python -m election.baselines <dir-with-*_General_Election_Returns.csv>      # writes out/election/baselines.json

Offices kept, by registry key: us-senate (2018, 2024), president (2024), governor / lt-governor / attorney-general /
comptroller / land-commissioner / ag-commissioner / railroad-commissioner (2018, 2022), sd-N and hd-N (2022, 2024;
same district lines as 2026). Congressional districts were redrawn in 2025, so no CD baselines.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import OUT_DIR

OFFICES = {
    "U.S. Sen": "us-senate", "President": "president", "Governor": "governor", "Lt. Governor": "lt-governor",
    "Attorney Gen": "attorney-general", "Comptroller": "comptroller", "Land Comm": "land-commissioner",
    "Ag Comm": "ag-commissioner", "RR Comm 1": "railroad-commissioner", "RR Comm": "railroad-commissioner",
}
YEARS = ("2018", "2022", "2024")


def office_key(office: str) -> str | None:
    if office in OFFICES:
        return OFFICES[office]
    m = re.match(r"^State Sen (\d+)$", office)
    if m:
        return f"sd-{int(m.group(1))}"
    m = re.match(r"^State Rep (\d+)$", office)
    if m:
        return f"hd-{int(m.group(1))}"
    return None


def build(src: Path) -> dict:
    out: dict = {"source": "Texas Legislative Council, Comprehensive Election Datasets (2024 VTD vintage), CC-BY; counties are sums of VTDs",
                 "years": list(YEARS), "offices": {}}
    for yr in YEARS:
        fn = src / f"{yr}_General_Election_Returns.csv"
        if not fn.exists():
            print(f"missing {fn}", file=sys.stderr)
            continue
        agg: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))   # key -> county -> party -> votes
        names: dict[str, dict[str, str]] = defaultdict(dict)                                                     # key -> party -> surname
        with fn.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = office_key(r.get("Office") or "")
                if not key:
                    continue
                party = (r.get("Party") or "").strip().upper()
                p = party if party in ("R", "D") else "O"
                try:
                    v = int(r.get("Votes") or 0)
                except ValueError:
                    continue
                agg[key][r["County"].strip()][p] += v
                if p != "O" and p not in names[key]:
                    names[key][p] = (r.get("Name") or "").strip()
        for key, counties in agg.items():
            rec = {"counties": {}, "state": {"R": 0, "D": 0, "O": 0}}
            for county, parties in counties.items():
                t = parties["R"] + parties["D"] + parties["O"]
                rec["counties"][county] = {"R": parties["R"], "D": parties["D"], "O": parties["O"], "T": t,
                                           "margin": round(100.0 * (parties["R"] - parties["D"]) / t, 2) if t else None}
                for p in ("R", "D", "O"):
                    rec["state"][p] += parties[p]
            st = rec["state"]
            st["T"] = st["R"] + st["D"] + st["O"]
            st["margin"] = round(100.0 * (st["R"] - st["D"]) / st["T"], 2) if st["T"] else None
            st["R_pct"] = round(100.0 * st["R"] / st["T"], 1) if st["T"] else None
            st["D_pct"] = round(100.0 * st["D"] / st["T"], 1) if st["T"] else None
            rec["label"] = " vs ".join(x for x in (names[key].get("R"), names[key].get("D")) if x) or None
            out["offices"].setdefault(key, {})[yr] = rec
    return out


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    data = build(src)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / "baselines.json"
    p.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {p} ({p.stat().st_size / 1e6:.2f} MB): {len(data['offices'])} offices")
    for key in ("us-senate", "governor", "lt-governor", "attorney-general", "comptroller", "president"):
        for yr, rec in sorted((data["offices"].get(key) or {}).items()):
            s = rec["state"]
            print(f"  {key:18s} {yr}  {rec['label']}: R {s['R_pct']}% D {s['D_pct']}% margin R{s['margin']:+.1f} total {s['T']:,}")


if __name__ == "__main__":
    main()
