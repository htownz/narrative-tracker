"""Account registry: one row per account across every tracker, with what their record says about believing them
and about following them.

  claims_first     canonical claims this account posted first (claims.json first_handle), across trackers
  claims_early     claims where it was among the first three posters
  verified         of the claims it posted first, how many have a verdict
  supported / unsupported / misleading
  reliability      supported share of its verified first-posted claims (None under MIN_VERIFIED verified claims)
  early_n / early_top5 / early_lift
                   claims it touched early that had a 24h outcome on a competitive day, the share that reached the
                   tracker's top five, and that share over the base rate (tracker/scorecard.py's MIN_FIELD applies)
  rings            suspicious link rings it took part in (tracker/rings.py)
  trackers, reach, posts, followers, account_age_years, role   from the amplifier map

build() -> out/accounts/registry.json; accounts_all() renders it on the amplifier map; features.py reads it to
stamp each claim-day with its first poster's reliability and early lift (first_reliability, first_early_lift).
A reliability of 0.4 means: of the claims this account put on X first that could be checked, 40% held up.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUT_DIR
from .store import profile_url

MIN_VERIFIED = 3
MIN_EARLY = 5
MIN_FIELD = 10


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def _feature_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fdir = DATA_DIR / "features"
    for f in sorted(fdir.glob("*.csv")) if fdir.exists() else []:
        if f.name == "picks.csv":
            continue
        with f.open(encoding="utf-8", newline="") as fh:
            rows += list(csv.DictReader(fh))
    return rows


def build(slugs: list[str] | None = None, feature_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    slugs = slugs if slugs is not None else sorted(d.name for d in OUT_DIR.iterdir() if d.is_dir() and (d / "claims.json").exists())
    acc: dict[str, dict[str, Any]] = {}

    def row(h: str) -> dict[str, Any]:
        return acc.setdefault(h, {"handle": h, "claims_first": 0, "claims_early": 0, "verified": 0, "supported": 0, "unsupported": 0, "misleading": 0,
                                  "unverifiable": 0, "rings": 0, "trackers": set(), "early": []})
    claims_by: dict[str, dict[str, dict[str, Any]]] = {}
    for slug in slugs:
        cj = _load(OUT_DIR / slug / "claims.json") or {}
        claims_by[slug] = {str(c["id"]): c for c in cj.get("clusters", [])}
        for c in claims_by[slug].values():
            firsts = c.get("first_handles") or ([c["first_handle"]] if c.get("first_handle") else [])
            if not firsts:
                continue
            r = row(firsts[0])
            r["claims_first"] += 1
            r["trackers"].add(slug)
            if c.get("verdict"):
                r["verified"] += 1
                r[c["verdict"]] = r.get(c["verdict"], 0) + 1
            for h in firsts[:3]:
                row(h)["claims_early"] += 1
                row(h)["trackers"].add(slug)
        rj = _load(OUT_DIR / slug / "rings.json") or {}
        for ring in rj.get("rings", []):
            if ring.get("suspicious"):
                for h in ring.get("handles", []):
                    row(h)["rings"] += 1
                    row(h)["trackers"].add(slug)
    # early touch -> outcome, one outcome per claim, competitive days only
    rows = feature_rows if feature_rows is not None else _feature_rows()
    field: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        if r["kind"] == "claim":
            field[(r["slug"], r["asof"])] += 1
    labelled = [r for r in rows if r["kind"] == "claim" and r["gain_24h"] not in ("", None) and field[(r["slug"], r["asof"])] >= MIN_FIELD]
    base = (sum(int(str(r["top5_24h"]) == "1") for r in labelled) / len(labelled)) if labelled else None
    seen: set[tuple[str, str]] = set()
    for r in sorted(labelled, key=lambda r: r["asof"]):
        key = (r["slug"], r["key"])
        if key in seen:
            continue
        seen.add(key)
        c = claims_by.get(r["slug"], {}).get(r["key"])
        if not c:
            continue
        firsts = c.get("first_handles") or ([c["first_handle"]] if c.get("first_handle") else [])
        for h in firsts[:3]:
            row(h)["early"].append(int(str(r["top5_24h"]) == "1"))
    # the amplifier map's numbers; per-tracker account tables fill in anyone the cross-tracker map does not list
    amp = {a["handle"]: a for a in (_load(OUT_DIR / "accounts" / "accounts.json") or [])}
    for slug in slugs:
        for a in _load(OUT_DIR / slug / "accounts.json") or []:
            cur = amp.get(a["handle"])
            if cur is None:
                amp[a["handle"]] = {**a, "reach": a.get("reach", 0), "posts": a.get("posts", 0), "_per_tracker": True}
            elif cur.get("_per_tracker"):
                cur["reach"] = (cur.get("reach") or 0) + (a.get("reach") or 0)
                cur["posts"] = (cur.get("posts") or 0) + (a.get("posts") or 0)
                cur["followers"] = max(cur.get("followers") or 0, a.get("followers") or 0)
    out = []
    for h, r in acc.items():
        a = amp.get(h, {})
        early = r.pop("early")
        p = (sum(early) / len(early)) if len(early) >= MIN_EARLY else None
        r.update({
            "trackers": sorted(r["trackers"]), "reliability": round(r["supported"] / r["verified"], 2) if r["verified"] >= MIN_VERIFIED else None,
            "early_n": len(early), "early_top5": round(p, 2) if p is not None else None,
            "early_lift": round(p / base, 1) if p is not None and base else None,
            "name": a.get("name"), "followers": a.get("followers"), "account_age_years": a.get("account_age_years"), "role": a.get("role"),
            "reach": a.get("reach", 0), "posts": a.get("posts", 0), "avatar": a.get("avatar"),
        })
        out.append(r)
    out.sort(key=lambda r: (-r["claims_first"], -r["claims_early"], -(r["reach"] or 0)))
    (OUT_DIR / "accounts").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "accounts" / "registry.json").write_text(json.dumps({"base_rate": round(base, 3) if base is not None else None, "min_verified": MIN_VERIFIED,
                                                                     "min_early": MIN_EARLY, "accounts": out}, ensure_ascii=False), encoding="utf-8")
    print(f"registry: {len(out)} accounts; {sum(1 for r in out if r['reliability'] is not None)} with a reliability score, "
          f"{sum(1 for r in out if r['early_lift'] is not None)} with an early-touch lift (base rate {base:.0%})" if base is not None else
          f"registry: {len(out)} accounts; no outcomes yet")
    return out


def lookup() -> dict[str, dict[str, Any]]:
    """handle -> registry row, from the last build (empty when none)."""
    data = _load(OUT_DIR / "accounts" / "registry.json") or {}
    return {r["handle"]: r for r in data.get("accounts", [])}


def table_html(rows: list[dict[str, Any]], base_rate: float | None, top_n: int = 100) -> str:
    """The registry as a table for the amplifier map: accounts with MIN_VERIFIED+ verified first posts or MIN_EARLY+ early touches."""
    import html
    shown = [r for r in rows if r["reliability"] is not None or r["early_lift"] is not None][:top_n]
    if not shown:
        return '<p class="sub">No account has enough verified first posts or scored early touches yet.</p>'
    trs = []
    for r in shown:
        rel = r["reliability"]
        tone = "#2fbf8c" if rel is not None and rel >= 0.7 else "#ff5252" if rel is not None and rel < 0.4 else "var(--ink)"
        rel_cell = f'<b style="color:{tone}">{rel:.0%}</b><div class="inc">{r["supported"]} of {r["verified"]} held up</div>' if rel is not None else f'<span class="inc">{r["verified"]} verified</span>'
        lift = r["early_lift"]
        lift_cell = (f'<b style="color:{"#e08a2e" if lift >= 2 else "var(--ink)"}">{lift}×</b><div class="inc">{r["early_top5"]:.0%} of {r["early_n"]}</div>'
                     if lift is not None else f'<span class="inc">{r["early_n"]} scored</span>')
        bad = r["unsupported"] + r["misleading"]
        trs.append(f'<tr><td><div class="nm"><a href="{html.escape(profile_url(r["handle"]))}" target="_blank" rel="noopener">@{html.escape(r["handle"])}</a></div>'
                   f'<div class="h">{_fmt(r["followers"] or 0)} followers · {len(r["trackers"])} tracker{"s" if len(r["trackers"]) != 1 else ""}'
                   f'{" · " + html.escape(r["role"]) if r.get("role") else ""}</div></td>'
                   f'<td class="n">{r["claims_first"]}</td><td class="n">{r["claims_early"]}</td><td class="n">{rel_cell}</td>'
                   f'<td class="n">{bad}<div class="inc">{r["unsupported"]} unsupported · {r["misleading"]} misleading</div></td>'
                   f'<td class="n">{lift_cell}</td><td class="n">{r["rings"] or ""}</td></tr>')
    return (f'<div class="wrap"><table><thead><tr><th>Account</th><th class="n">Posted first</th><th class="n">In first three</th><th class="n">Held up</th>'
            f'<th class="n">Did not</th><th class="n">Early-touch lift</th><th class="n">Rings</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
            f'<p class="sub">Held up: the share of this account\'s verified first-posted claims found supported ({MIN_VERIFIED}+ verified). Early-touch lift: how often a claim it was '
            f'among the first three to post reached its tracker\'s top five within 24h, over the base rate{f" of {base_rate:.0%}" if base_rate else ""} ({MIN_EARLY}+ scored). '
            f'Rings: suspicious link rings it posted in. Feeds the feature table as first_reliability and first_early_lift.</p>')


def _fmt(n: int | float) -> str:
    n = float(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else str(int(n))
