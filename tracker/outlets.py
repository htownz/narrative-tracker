"""Outlet lean labels (incidents/outlets.yaml): which side of the press carried a claim.

lean(domain) -> 'left' | 'lean-left' | 'center' | 'lean-right' | 'right' | 'unrated'
side(lean)   -> 'L' | 'C' | 'R' | '?'   (lean-left folds into L, lean-right into R)
mix(domains) -> counts by side, plus 'one_sided' when every rated outlet sits on one side
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from .config import ROOT

OUTLETS_PATH = ROOT / "incidents" / "outlets.yaml"
SIDES = {"left": "L", "lean-left": "L", "center": "C", "lean-right": "R", "right": "R"}


@lru_cache(maxsize=1)
def table() -> dict[str, dict[str, Any]]:
    if not OUTLETS_PATH.exists():
        return {}
    raw = yaml.safe_load(OUTLETS_PATH.read_text(encoding="utf-8")) or {}
    out = {}
    for o in raw.get("outlets") or []:
        d = str(o.get("domain") or "").lower().removeprefix("www.")
        if d:
            out[d] = {"name": o.get("name") or d, "lean": str(o.get("lean") or "unrated"), "texas": bool(o.get("texas"))}
    return out


def _norm(domain: str) -> str:
    d = (domain or "").lower().split("/")[0].removeprefix("www.")
    return d


def info(domain: str) -> dict[str, Any]:
    t = table()
    d = _norm(domain)
    if d in t:
        return t[d]
    parts = d.split(".")                                      # news.google.com, amp.texastribune.org
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in t:
            return t[".".join(parts[i:])]
    return {"name": d, "lean": "unrated", "texas": False}


def lean(domain: str) -> str:
    return info(domain)["lean"]


def side(lean_: str) -> str:
    return SIDES.get(lean_, "?")


def mix(domains: list[str]) -> dict[str, Any]:
    """Counts of distinct outlets by side for a claim's coverage."""
    seen = {_norm(d) for d in domains if d}
    counts = {"L": 0, "C": 0, "R": 0, "?": 0}
    texas = 0
    for d in seen:
        i = info(d)
        counts[side(i["lean"])] += 1
        texas += int(i["texas"])
    rated = counts["L"] + counts["C"] + counts["R"]
    one_sided = None
    if rated >= 2:
        if counts["L"] and not counts["R"]:
            one_sided = "L"
        elif counts["R"] and not counts["L"]:
            one_sided = "R"
    return {**counts, "rated": rated, "texas": texas, "one_sided": one_sided}
