"""Message-doc phrases: the anchor lines of a coordinated campaign, counted wherever they appear.

An incident YAML may list `phrases:` — the lines a message document hands to its carriers ("largest turnaround
in Texas history", "no student attends a failing school"). For each phrase this counts, over the last DAYS days,
the posts in the tracker's store (every platform) and the press articles in data/news.sqlite that carry it, by
day and in total, with the accounts and outlets that used it. That is the HISD sweep's "message-doc language
flags", automated: a phrase that appears in a think-tank column, then in the Governor's office's post, then in
twenty small accounts within a day is a campaign showing its wiring.

phrases(inc) -> out/<slug>/phrases.json; section_html(data) for the analysis page.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Incident
from . import newsfeeds
from .store import Store

DAYS = 14


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\s+", " ", (text or "").lower()))


def _matcher(phrase: str):
    """Case-insensitive, punctuation-insensitive; digits and words must appear in order with any spacing."""
    toks = _norm(phrase).split()
    pat = r"\b" + r"\s+".join(re.escape(t) for t in toks) + r"\b"
    return re.compile(pat)


def count(inc: Incident, days: int = DAYS, now: datetime | None = None) -> dict[str, Any]:
    phrases = [str(p) for p in (inc.raw.get("phrases") or []) if str(p).strip()]
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    out: dict[str, Any] = {"generated_at": now.isoformat(), "slug": inc.slug, "days": days, "phrases": []}
    if not phrases:
        return out
    store = Store(inc.db_path)
    posts = [dict(r) for r in store.conn.execute("SELECT id, created_at, author_handle, author_followers, view_count, text, url, source, lane FROM posts WHERE created_at >= ?", (since,))]
    store.close()
    articles: list[dict[str, Any]] = []
    if newsfeeds.DB_PATH.exists():
        try:
            conn = newsfeeds.connect()
            articles = [dict(r) for r in conn.execute("SELECT url, domain, title, summary, published FROM articles WHERE published >= ?", (since,))]
            conn.close()
        except sqlite3.Error:
            articles = []
    lanes = {l["key"]: l["label"] for l in inc.lane_table()}
    for ph in phrases:
        m = _matcher(ph)
        hits = [p for p in posts if m.search(_norm(p["text"]))]
        arts = [a for a in articles if m.search(_norm(f"{a['title']} {a['summary']}"))]
        by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"posts": 0, "articles": 0})
        for p in hits:
            by_day[p["created_at"][:10]]["posts"] += 1
        for a in arts:
            by_day[(a["published"] or "")[:10]]["articles"] += 1
        accounts: dict[str, dict[str, Any]] = {}
        for p in hits:
            a = accounts.setdefault(p["author_handle"], {"handle": p["author_handle"], "posts": 0, "views": 0, "followers": int(p["author_followers"] or 0), "first": p["created_at"]})
            a["posts"] += 1; a["views"] += int(p["view_count"] or 0); a["first"] = min(a["first"], p["created_at"])
        top_accounts = sorted(accounts.values(), key=lambda a: -a["views"])[:8]
        first_post = min(hits, key=lambda p: p["created_at"]) if hits else None
        first_art = min(arts, key=lambda a: a["published"] or "9") if arts else None
        lane_counts: dict[str, int] = defaultdict(int)
        for p in hits:
            lane_counts[lanes.get(p["lane"], p["lane"] or "unlabelled")] += 1
        out["phrases"].append({
            "phrase": ph, "posts": len(hits), "accounts": len(accounts), "views": sum(int(p["view_count"] or 0) for p in hits),
            "articles": len(arts), "outlets": len({a["domain"] for a in arts}),
            "by_day": [{"day": d, **by_day[d]} for d in sorted(by_day)],
            "first_post": {"at": first_post["created_at"], "handle": first_post["author_handle"], "url": first_post["url"]} if first_post else None,
            "first_article": {"at": first_art["published"], "domain": first_art["domain"], "title": first_art["title"], "url": first_art["url"]} if first_art else None,
            "top_accounts": top_accounts, "lanes": sorted(lane_counts.items(), key=lambda kv: -kv[1])[:3],
            "sources": sorted({p["source"] or "x" for p in hits}), "outlet_list": sorted({a["domain"] for a in arts})[:10],
        })
    out["phrases"].sort(key=lambda r: -(r["posts"] + r["articles"]))
    return out


def phrases(inc: Incident) -> Path | None:
    data = count(inc)
    if not data["phrases"]:
        return None
    path = inc.out_dir / "phrases.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}  (" + ", ".join(f"{r['phrase'][:30]!r}: {r['posts']} posts/{r['articles']} articles" for r in data["phrases"][:4]) + ")")
    return path


def section_html(data: dict[str, Any] | None) -> str:
    if not data or not data.get("phrases"):
        return ""
    trs = []
    for r in data["phrases"]:
        accs = ", ".join(f'@{html.escape(a["handle"])} ({a["posts"]})' for a in r["top_accounts"][:5])
        first = ""
        if r["first_post"] and r["first_article"]:
            fp, fa = r["first_post"]["at"][:10], (r["first_article"]["at"] or "")[:10]
            first = f'press first ({fa}, {html.escape(r["first_article"]["domain"])})' if fa and fa < fp else f'X first ({fp}, @{html.escape(r["first_post"]["handle"])})'
        elif r["first_post"]:
            first = f'X only, first @{html.escape(r["first_post"]["handle"])} {r["first_post"]["at"][:10]}'
        elif r["first_article"]:
            first = f'press only, first {html.escape(r["first_article"]["domain"])} {(r["first_article"]["at"] or "")[:10]}'
        spark = " ".join(f'{d["day"][5:]}:{d["posts"]}/{d["articles"]}' for d in r["by_day"][-7:])
        trs.append(f'<tr><td><b>{html.escape(r["phrase"])}</b><div class="muted">{html.escape(", ".join(l for l, _ in r["lanes"]))}</div></td>'
                   f'<td class="n">{r["posts"]}</td><td class="n">{r["accounts"]}</td><td class="n">{r["views"]:,}</td><td class="n">{r["articles"]}</td>'
                   f'<td>{html.escape(first)}<div class="muted">{accs}</div><div class="muted">{html.escape(", ".join(r["outlet_list"][:5]))}</div></td><td class="muted">{html.escape(spark)}</td></tr>')
    return (f'<h2>Message-doc phrases</h2><p class="sub">The anchor lines a coordinated campaign hands its carriers (from the incident YAML), counted over the last {data["days"]} days '
            f'in every post in this tracker and every article in the press store: who used them, which came first, and the day-by-day (posts/articles).</p>'
            '<div class="wrap"><table><thead><tr><th>Phrase</th><th class="n">Posts</th><th class="n">Accounts</th><th class="n">Views</th><th class="n">Articles</th><th>Who, and who was first</th><th>Last 7 days</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')
