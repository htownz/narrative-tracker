"""The weekly brief: what moved on X in Texas politics this week, written from the trackers' own numbers.

facts(days=7)   -> per tracker: volume vs the prior week, lanes that gained or faded, the week's biggest claims
                   with verdicts, claims that failed verification, who originated and who carried, link rings and
                   control flags, market move, events and the lanes that answered them
write(facts)    -> Claude turns the facts into plain prose (no number that is not in the facts), sectioned
brief()         -> out/brief/<date>.html + out/brief/latest.json (+ index); Telegram summary when configured

    python run.py brief            # the week ending now
    python run.py brief --days 14
The job runs it on the Monday 11:17 UTC run and whenever `.run` contains the word `brief`.
"""
from __future__ import annotations

import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import OUT_DIR, PUSHBACK_KEY, env, incident_paths, load_incident
from .lanes import _client
from .store import Store

BRIEF_DIR = OUT_DIR / "brief"
TZ = ZoneInfo("America/Chicago")

SYSTEM = """You write a weekly brief for a Texas government-affairs firm about how political stories moved on X.
Plain, professional language: short declarative sentences, concrete numbers, named accounts and claims, no
rhetoric, no adjectives that do work a number could do. Every figure, name, claim and verdict you use must come
from the facts you are given — never add a fact, and never soften or sharpen a verdict. Write for a reader who
has five minutes and knows Texas politics. Round views to millions or thousands (171.7 million, 59K), write
account names as @handle, and call each tracker by its subject (the Senate race, the governor's race, Trump's
Texas footprint) rather than "the tracker". Do not describe the tool or its methods."""

USER = """Week: {start} to {end} (America/Chicago). Facts, as JSON, one object per tracker:

{facts}

Write the brief as JSON with these keys:
  "headline": one sentence, the single most important thing that happened on X in Texas politics this week.
  "summary": 3-5 sentences that stand on their own (this goes out as a text message).
  "sections": a list of {{"title": ..., "body": ...}} — one per tracker that had meaningful activity (skip trackers
      with almost nothing; group the issue trackers into one section called "Issues"), each body 2-6 short
      paragraphs separated by blank lines. Lead each with what changed versus the prior week. Name the biggest
      claims and say which were verified unsupported or misleading and on what basis. Name who originated and who
      carried. Where "electeds" lists posts by officials, candidates or agencies, say what they said and into which
      lane, in one or two sentences — rhetoric from office-holders is what the firm's clients act on. Mention
      coordinated pushes or link rings only when the facts flag them. Note the market move in one clause when it
      is there.
  "watch": 3-5 bullet strings: what to watch next week, each tied to a fact (an event on the calendar, a claim
      still gaining, a lane that is rising).
Return JSON only."""

def _window_start_utc(inc) -> str:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()



def _week_bounds(days: int, end: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    return prev_start, start, end


def _load(slug: str, name: str) -> dict[str, Any]:
    p = OUT_DIR / slug / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _pct(a: float, b: float) -> float | None:
    return round((a - b) / b, 2) if b else None


def tracker_facts(inc, prev_start: datetime, start: datetime, end: datetime) -> dict[str, Any] | None:
    store = Store(inc.db_path)
    lanes = {l["key"]: l["label"] for l in inc.lane_table()}
    posts = [p for p in store.chart_posts(inc.min_views, _window_start_utc(inc)) if p["lane"] in lanes]
    def ts(p):
        return datetime.fromisoformat(p["created_at"])
    this = [p for p in posts if start <= ts(p) < end]
    prev = [p for p in posts if prev_start <= ts(p) < start]
    if len(this) < 5:
        store.close()
        return None
    views = lambda ps: sum(int(p["view_count"] or 0) for p in ps)  # noqa: E731
    # lanes: views this week vs last, share
    lv_this, lv_prev = defaultdict(int), defaultdict(int)
    for p in this:
        lv_this[p["lane"]] += int(p["view_count"] or 0)
    for p in prev:
        lv_prev[p["lane"]] += int(p["view_count"] or 0)
    tot = views(this) or 1
    lane_rows = [{"lane": lanes[k], "views": v, "share": round(v / tot, 2), "change_vs_prior_week": _pct(v, lv_prev.get(k, 0))}
                 for k, v in sorted(lv_this.items(), key=lambda kv: -kv[1])]
    # accounts: originators and carriers this week (views of their own posts; downstream from quotes/copies)
    own, down = defaultdict(int), defaultdict(int)
    by_id = {p["id"]: p for p in posts}
    for p in this:
        own[p["author_handle"]] += int(p["view_count"] or 0)
        for key in ("quoted_id", "in_reply_to_id", "copy_origin"):
            par = by_id.get(p.get(key) or "")
            if par and par["author_handle"] != p["author_handle"]:
                down[par["author_handle"]] += int(p["view_count"] or 0)
    top_accounts = [{"handle": h, "own_views": own[h], "downstream_views": down.get(h, 0), "posts": sum(1 for p in this if p["author_handle"] == h)}
                    for h in sorted(own, key=lambda h: -(own[h] + down.get(h, 0)))[:8]]
    # claims: canonical claims with posts this week
    cj = _load(inc.slug, "claims.json")
    week_claims = []
    for c in cj.get("clusters", []):
        wk = [v for t, v in c.get("series", []) if start.isoformat() <= t < end.isoformat()]
        if not wk:
            continue
        week_claims.append({"claim": c["text"], "views_this_week": sum(wk), "posts_this_week": len(wk), "views_total": c["views"],
                            "first_seen": c["first_seen"][:10], "first_by": c["first_handle"], "new_this_week": c["first_seen"] >= start.isoformat(),
                            "type": c["type"], "verdict": c.get("verdict"), "verdict_basis": (c.get("verdict_basis") or "")[:300] or None,
                            "lanes": [l["label"] for l in c.get("lanes", [])[:2]], "after_event": c.get("event")})
    week_claims.sort(key=lambda r: -r["views_this_week"])
    failed = [c for c in week_claims if c["verdict"] in ("unsupported", "misleading")]
    # control + rings
    ctrl = _load(inc.slug, "control.json")
    flagged = [{"what": r["label"], "kind": r["kind"], "score": r["score"], "why": r["why"]} for r in ctrl.get("flagged", [])[:3]]
    rj = _load(inc.slug, "rings.json")
    rings = [{"url": r["url"], "accounts_in_burst": r["burst_accounts"], "views": r["views"], "suspicious": bool(r.get("suspicious")),
              "handles": r["handles"][:5]} for r in rj.get("rings", []) if start.isoformat() <= r["burst_start"] < end.isoformat()][:4]
    # market
    dj = _load(inc.slug, "data.json")
    market = None
    for m in dj.get("markets") or []:
        pts = [q for q in m.get("points", []) if start.timestamp() * 1000 <= q["t"] <= end.timestamp() * 1000]
        if len(pts) >= 2:
            market = {"label": m["label"], "start_cents": pts[0]["p"], "end_cents": pts[-1]["p"], "low": min(q["p"] for q in pts), "high": max(q["p"] for q in pts)}
    # events + response
    aj = _load(inc.slug, "analysis.json")
    events = []
    for e in aj.get("events", []):
        if start.isoformat() <= e["at"] < end.isoformat():
            top = e.get("top_lane")
            events.append({"event": e["label"], "at": e["at"][:16], "views_12h_after_vs_before": (round(e["ratio"], 1) if isinstance(e.get("ratio"), (int, float)) and e["ratio"] != float("inf") else "new"),
                           "top_lane": lanes.get(top, top)})
    # alerts this week
    al = _load(inc.slug, "alerts.json")
    alerts = [{"type": a["type"], "title": a["title"]} for a in al.get("alerts", [])[:3]]
    store.close()
    return {
        "tracker": inc.title.split(",")[0], "slug": inc.slug, "subject": inc.subject,
        "posts_this_week": len(this), "posts_prior_week": len(prev), "views_this_week": views(this), "views_prior_week": views(prev),
        "views_change": _pct(views(this), views(prev)), "pushback_share": round(lv_this.get(PUSHBACK_KEY, 0) / tot, 2),
        "lanes": lane_rows[:7], "top_accounts": top_accounts, "biggest_claims": week_claims[:6], "failed_verification": failed[:5],
        "new_claims_this_week": sum(1 for c in week_claims if c["new_this_week"]), "coordinated_pushes": flagged, "link_rings": rings,
        "market": market, "events": events, "alerts": alerts,
        "electeds": [{"who": f"{r['name']} ({r['office']})", "lane": r["lane"], "views": r["views"], "post": r["text"][:200]} for r in __import__("tracker.officials", fromlist=["said"]).said(inc.slug, days=7, end=end)[:6]],
    }


def facts(days: int = 7, end: datetime | None = None) -> dict[str, Any]:
    prev_start, start, end = _week_bounds(days, end)
    out = []
    for y in incident_paths():
        inc = load_incident(y)
        if not inc.live or not inc.db_path.exists() or not inc.lanes:
            continue
        f = tracker_facts(inc, prev_start, start, end)
        if f:
            out.append(f)
    out.sort(key=lambda f: -f["views_this_week"])
    return {"start": start.astimezone(TZ).strftime("%b %d"), "end": end.astimezone(TZ).strftime("%b %d, %Y"),
            "start_iso": start.isoformat(), "end_iso": end.isoformat(), "days": days, "trackers": out}


def write(fx: dict[str, Any], model: str = "claude-sonnet-5-5") -> dict[str, Any]:
    from .lanes import _ask
    client = _client()
    slim = json.dumps(fx["trackers"], ensure_ascii=False)
    res = _ask(client, model, SYSTEM, USER.format(start=fx["start"], end=fx["end"], facts=slim), max_tokens=16000)   # ~8K tokens of prose
    if not res.get("sections"):
        raise SystemExit("brief: the model returned no sections")
    return {"headline": str(res.get("headline") or ""), "summary": str(res.get("summary") or ""),
            "sections": [{"title": str(s.get("title") or ""), "body": str(s.get("body") or "")} for s in res["sections"] if isinstance(s, dict)],
            "watch": [str(w) for w in (res.get("watch") or [])][:6]}


CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e; }
  * { box-sizing:border-box; } body { margin:0 auto; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; padding:16px 16px 48px; max-width:820px; line-height:1.5; }
  h1 { font-size:24px; margin:0 0 4px; line-height:1.25; } .when { color:var(--ink-3); font-size:12px; margin:0 0 18px; } h2 { font-size:15px; margin:26px 0 8px; color:var(--accent); }
  .lead { font-size:15.5px; color:var(--ink); background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  p { margin:0 0 12px; font-size:14.5px; } ul { padding-left:20px; } li { margin:4px 0; font-size:14px; }
  .facts { margin-top:28px; border-top:1px solid var(--line); padding-top:12px; } .facts summary { cursor:pointer; color:var(--ink-3); font-size:12px; }
  table { border-collapse:collapse; width:100%; font-size:12px; margin:8px 0 14px; } th { text-align:left; color:var(--ink-3); font-weight:600; font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; padding:5px 6px; border-bottom:1px solid var(--line); }
  td { padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; } td.n { text-align:right; font-family:ui-monospace,Menlo,Consolas,monospace; }
  .pill { font-size:10px; text-transform:uppercase; letter-spacing:.04em; border:1px solid var(--line); border-radius:999px; padding:1px 6px; color:var(--ink-2); } .pill.bad { color:#ffb3b3; border-color:#ff5252; }
  footer { margin-top:20px; color:var(--ink-3); font-size:11px; } a { color:#8cf; } .archive a { color:var(--ink-2); }
"""


def _fmt(n: float) -> str:
    return f"{n/1e6:.1f}M".replace(".0M", "M") if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else f"{int(n)}"


def render(fx: dict[str, Any], text: dict[str, Any], archive: list[str]) -> str:
    secs = "".join(f"<h2>{html.escape(s['title'])}</h2>" + "".join(f"<p>{html.escape(par.strip())}</p>" for par in s["body"].split("\n\n") if par.strip())
                   for s in text["sections"])
    watch = "".join(f"<li>{html.escape(w)}</li>" for w in text["watch"])
    tables = []
    for t in fx["trackers"]:
        lanes = "".join(f'<tr><td>{html.escape(l["lane"])}</td><td class="n">{_fmt(l["views"])}</td><td class="n">{l["share"]:.0%}</td><td class="n">{("%+.0f%%" % (l["change_vs_prior_week"]*100)) if l["change_vs_prior_week"] is not None else "new"}</td></tr>' for l in t["lanes"])
        def pill(c):
            if not c["verdict"]:
                return ""
            cls = "pill bad" if c["verdict"] in ("unsupported", "misleading") else "pill"
            return f' <span class="{cls}">{html.escape(c["verdict"])}</span>'
        claims = "".join(f'<tr><td>{html.escape(c["claim"])}{pill(c)}</td><td class="n">{_fmt(c["views_this_week"])}</td><td>@{html.escape(c["first_by"])}</td></tr>' for c in t["biggest_claims"][:5])
        accts = ", ".join(f'@{html.escape(a["handle"])} ({_fmt(a["own_views"] + a["downstream_views"])})' for a in t["top_accounts"][:6])
        mk = f' · {html.escape(t["market"]["label"])} {t["market"]["start_cents"]:.0f}¢ → {t["market"]["end_cents"]:.0f}¢' if t.get("market") else ""
        tables.append(f"""<h3 style="font-size:13px;margin:16px 0 4px">{html.escape(t["tracker"])} <span style="color:var(--ink-3);font-weight:400">— {t["posts_this_week"]:,} posts, {_fmt(t["views_this_week"])} views ({("%+.0f%%" % (t["views_change"]*100)) if t["views_change"] is not None else "new"} vs prior week), pushback {t["pushback_share"]:.0%}{mk}</span></h3>
<table><thead><tr><th>Lane</th><th class="n">Views</th><th class="n">Share</th><th class="n">vs prior</th></tr></thead><tbody>{lanes}</tbody></table>
<table><thead><tr><th>Biggest claims this week</th><th class="n">Views</th><th>First by</th></tr></thead><tbody>{claims}</tbody></table>
<p style="font-size:12px;color:var(--ink-2)">Moved it: {accts}</p>""")
    arch = " · ".join(f'<a href="{html.escape(a)}.html">{html.escape(a)}</a>' for a in archive[:12])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Texas narrative brief, week of {html.escape(fx["end"])}</title><style>{CSS}</style></head><body>
<p class="when">Texas Narrative Trackers · weekly brief · {html.escape(fx["start"])} – {html.escape(fx["end"])}</p>
<h1>{html.escape(text["headline"])}</h1>
<div class="lead">{html.escape(text["summary"])}</div>
{secs}
<h2>What to watch</h2><ul>{watch}</ul>
<details class="facts"><summary>The numbers behind this brief ({len(fx["trackers"])} trackers)</summary>{"".join(tables)}</details>
<footer>Written by Claude from the trackers' data for the week; verdicts link to their sources on each tracker's claims page. <span class="archive">Earlier briefs: {arch or "none yet"}</span></footer>
</body></html>"""


def brief(days: int = 7, send: bool = True) -> Path:
    fx = facts(days)
    if not fx["trackers"]:
        raise SystemExit("brief: no tracker had activity this week")
    if not env("ANTHROPIC_API_KEY"):
        raise SystemExit("brief: ANTHROPIC_API_KEY is not set")
    text = write(fx)
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    archive = sorted((p.stem for p in BRIEF_DIR.glob("20*.html") if p.stem != day), reverse=True)
    page = render(fx, text, archive)
    (BRIEF_DIR / f"{day}.html").write_text(page, encoding="utf-8")
    (BRIEF_DIR / "index.html").write_text(page, encoding="utf-8")
    (BRIEF_DIR / "latest.json").write_text(json.dumps({"date": day, "window": {"start": fx["start_iso"], "end": fx["end_iso"]}, **text,
                                                       "trackers": [{k: v for k, v in t.items() if k != "biggest_claims"} for t in fx["trackers"]]}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {BRIEF_DIR / (day + '.html')}  ({len(text['sections'])} sections, {len(fx['trackers'])} trackers)\n  {text['headline']}")
    if send:
        from .alerts import _telegram
        site = env("SITE_URL") or "https://htownz.github.io/narrative-tracker"
        _telegram(f"Texas narrative brief, {fx['start']}–{fx['end']}\n\n{text['headline']}\n\n{text['summary']}\n\n{site}/brief/")
    return BRIEF_DIR / f"{day}.html"
