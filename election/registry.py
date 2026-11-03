"""Builds the tracked-race list: the SOS feed's contests keyed by election/sos.race_key, with the registry.yaml
overlay (report framing, baselines) merged in. Any keyed contest in the feed is tracked even if the YAML is
silent about it; the YAML never invents a contest the feed lacks (local races live under `local:`)."""
from __future__ import annotations

import re
from typing import Any

import yaml

from . import REGISTRY_PATH
from .sos import race_key


def load_registry() -> dict[str, Any]:
    with REGISTRY_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def chamber_of(key: str) -> str:
    if key == "us-senate":
        return "us-senate"
    if key.startswith("cd-"):
        return "us-house"
    if key.startswith("sd-"):
        return "tx-senate"
    if key.startswith("hd-"):
        return "tx-house"
    if key.startswith("sboe-"):
        return "sboe"
    if key.startswith(("scotx-", "cca-")):
        return "courts"
    return "statewide"


def default_title(key: str, sos_name: str) -> str:
    m = re.match(r"^(cd|sd|hd|sboe)-(\d+)$", key)
    if m:
        kind, n = m.group(1), m.group(2)
        return {"cd": f"U.S. House District {n}", "sd": f"Texas Senate District {n}", "hd": f"Texas House District {n}",
                "sboe": f"State Board of Education District {n}"}[kind]
    return sos_name.title().replace("U. S.", "U.S.")


def default_report_section(key: str) -> str:
    return {"us-senate": "Federal", "us-house": "Congress", "tx-senate": "Texas Senate", "tx-house": "Texas House",
            "sboe": "State Board of Education", "courts": "Statewide", "statewide": "Statewide"}[chamber_of(key)]


def tracked_races(snap: dict[str, Any], reg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """race key -> merged race record (feed contest + overlay). Keeps the normalized candidates list."""
    reg = reg or load_registry()
    overlay = reg.get("races") or {}
    sections = set((reg.get("defaults") or {}).get("track_sections") or ["Federal", "StateWide", "Districted"])
    out: dict[str, dict[str, Any]] = {}
    for sos_name, r in snap["races"].items():
        if r["section"] not in sections:
            continue
        key = race_key(sos_name)
        if not key:
            continue
        o = overlay.get(key) or {}
        rec = {
            "key": key,
            "sos_id": r["sos_id"],
            "sos_name": sos_name,
            "name": sos_name,                       # the feed's contest name (county rows are matched on it)
            "section": r["section"],
            "title": o.get("title") or default_title(key, sos_name),
            "chamber": chamber_of(key),
            "competitive": bool(o.get("competitive")),
            "open_seat": bool(o.get("open_seat")),
            "no_incumbent_on_ballot": not any(c["incumbent"] for c in r["candidates"]),   # from the feed's "(I)" marks
            "region": o.get("region"),
            "report_section": o.get("report_section") or default_report_section(key),
            "note": o.get("note"),
            "meta": {**{k: v for k, v in o.items() if k not in {"title", "competitive", "open_seat", "region", "report_section", "note"}},
                     "no_incumbent_on_ballot": not any(c["incumbent"] for c in r["candidates"])},
            "candidates": r["candidates"],
            "total": r["total"],
        }
        out[key] = rec
    return out


def sort_key(key: str) -> tuple:
    order = {"us-senate": 0, "statewide": 1, "courts": 2, "us-house": 3, "tx-senate": 4, "tx-house": 5, "sboe": 6}
    m = re.search(r"(\d+)$", key)
    return (order.get(chamber_of(key), 9), key.split("-")[0], int(m.group(1)) if m else 0, key)
