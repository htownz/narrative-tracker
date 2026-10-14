"""Verification of checkable claims: Claude with web search gives each big factual claim a verdict.

verify(inc)   canonical claims that are checkable facts, at verify.min_views+ views and not yet verified,
              biggest first, verify.per_run per run -> verdict, basis and sources stored on the cluster;
              claims.html shows them, the hub lists unsupported claims that are still gaining views.

Verdicts: supported (accurate as stated) · unsupported (contradicted by reporting or records, or a specific
assertion with no evidence behind it) · misleading (true in part, framed to mislead or missing context) ·
unverifiable (cannot be settled from public sources). They are advisory and always shown with their sources.

YAML (optional):
  verify: { min_views: 100000, per_run: 5, max_searches: 5, model: claude-sonnet-5-5 }
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

from .config import Incident, env
from .lanes import _client, _json_from
from .store import Store

VERDICTS = {"supported", "unsupported", "misleading", "unverifiable"}

SYSTEM = """You verify claims that are circulating on X about Texas politics for a non-partisan monitoring tool.
Use web search to find primary records (bills, votes, filings, court documents, official statements, poll
releases) and reporting from established news organisations. Judge the claim as it is worded. Be strict about
specifics: a number, a date, a quote or an attribution that is wrong makes the claim unsupported or misleading
even when the gist is defensible. Do not reward or punish either side; the verdict is about accuracy."""

USER = """Claim (as made on X): "{claim}"
First seen on X: {first_seen}. Context: {subject}.

Search, then answer in two parts:
1. Basis: two to four sentences saying what the records or reporting show and how that bears on the claim, naming the sources.
2. A final line that is only JSON: {{"verdict": "supported|unsupported|misleading|unverifiable", "sources": ["url", ...]}}"""


def _settings(inc: Incident) -> dict[str, Any]:
    v = dict(inc.raw.get("verify") or {})
    return {"min_views": int(v.get("min_views", 100_000)), "per_run": int(v.get("per_run", 5)),
            "max_searches": int(v.get("max_searches", 5)), "model": v.get("model") or inc.classifier.get("model", "claude-sonnet-5-5")}


def candidates(store: Store, min_views: int, limit: int) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT cl.id, cl.text, cl.type, cl.checkable, MIN(p.created_at) first_seen, SUM(p.view_count) views, COUNT(DISTINCT p.id) posts "
        "FROM claim_clusters cl JOIN claims c ON c.cluster_id = cl.id JOIN posts p ON p.id = c.post_id "
        "WHERE cl.verdict IS NULL AND cl.checkable = 1 AND cl.type = 'fact' "
        "AND cl.text NOT LIKE 'The author%' AND cl.text NOT LIKE 'The poster%' "
        "GROUP BY cl.id HAVING views >= ? ORDER BY views DESC LIMIT ?", (min_views, limit)).fetchall()
    return [dict(r) for r in rows]


def ask(client, model: str, claim: str, first_seen: str, subject: str, max_searches: int) -> dict[str, Any]:
    """One verification call. Returns {verdict, basis, sources}; verdict 'unverifiable' when the reply cannot be parsed."""
    msg = client.messages.create(
        model=model, max_tokens=2000, system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
        messages=[{"role": "user", "content": USER.format(claim=claim, first_seen=first_seen[:10], subject=subject)}],
    )
    texts, cited = [], []
    for b in msg.content:
        if getattr(b, "type", "") == "text":
            texts.append(b.text)
            for c in getattr(b, "citations", None) or []:
                u = getattr(c, "url", None)
                if u and u not in cited:
                    cited.append(u)
    full = "\n".join(texts).strip()
    verdict, sources = "unverifiable", []
    m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", full, flags=re.S)
    if m:
        try:
            j = _json_from(m.group(0))
            v = str(j.get("verdict", "")).lower().strip()
            verdict = v if v in VERDICTS else "unverifiable"
            sources = [str(u) for u in (j.get("sources") or []) if isinstance(u, str)]
        except (ValueError, json.JSONDecodeError):
            pass
        basis = full[:m.start()].strip()
    else:
        basis = full
    basis = basis.replace("**", "")
    basis = re.sub(r"^\s*(1\.\s*)?Basis:?\s*", "", basis, flags=re.I).strip()
    basis = re.sub(r"\n\s*2\.\s*$", "", basis).strip()
    for u in cited:
        if u not in sources:
            sources.append(u)
    return {"verdict": verdict, "basis": basis[:1200], "sources": sources[:8]}


def verify(inc: Incident) -> int:
    if not env("ANTHROPIC_API_KEY"):
        print("verify: ANTHROPIC_API_KEY not set — skipping", file=sys.stderr)
        return 0
    st = _settings(inc)
    store = Store(inc.db_path)
    todo = candidates(store, st["min_views"], st["per_run"])
    if not todo:
        print("verify: nothing to verify")
        store.close()
        return 0
    client = _client()
    done = 0
    for c in todo:
        try:
            r = ask(client, st["model"], c["text"], c["first_seen"] or "", inc.subject, st["max_searches"])
        except Exception as e:  # web search unavailable on this key/model, network, etc.: say so, keep the claim for next time
            print(f"verify: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
            break
        store.set_verdict(int(c["id"]), r["verdict"], r["basis"], r["sources"], datetime.now(timezone.utc).isoformat())
        done += 1
        print(f"  {r['verdict']:<12} {c['text'][:90]}  ({int(c['views']):,} views, {len(r['sources'])} sources)")
    store.close()
    print(f"verify: {done} claims verified this run ({len(todo) - done} left for next run)")
    return done
