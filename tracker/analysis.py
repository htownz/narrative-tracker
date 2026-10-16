"""Analysis page: narrative timing, pushback lag and effectiveness, and lane-velocity vs market price.

    python run.py analysis incidents/x.yaml   -> out/<slug>/analysis.json + analysis.html

Per lane:
  first post, peak hour, half-life (hours from first post until half of the lane's eventual views had posted),
  first pushback aimed at the lane (a pushback post quoting/replying to a lane post), lag in hours,
  pushback share (views of pushback aimed at the lane / lane views).
Then: lanes that drew pushback inside 6 hours vs later or never — mean half-life and mean views of each group.
Then: per-lane lead/lag of hourly lane views against the market's hourly price change (from tracker.markets).
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PUSHBACK_KEY, Incident
from .markets import lead_lag

EARLY_H = 6


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def lane_timing(data: dict[str, Any]) -> list[dict[str, Any]]:
    posts = [p for p in data["posts"] if p.get("lane")]
    by_id = {p["id"]: p for p in posts}
    lanes = [l for l in data["lanes"] if l["key"] != PUSHBACK_KEY]
    # pushback aimed at a lane: pushback post whose quote/reply target is in that lane
    aimed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in data["edges"]:
        if e["type"] not in ("quote", "reply"):
            continue
        src, dst = by_id.get(e["src"]), by_id.get(e["dst"])
        if src and dst and src["lane"] == PUSHBACK_KEY and dst["lane"] != PUSHBACK_KEY:
            aimed[dst["lane"]].append(src)
    rows = []
    for l in lanes:
        lp = sorted((p for p in posts if p["lane"] == l["key"]), key=lambda p: p["t"])
        if not lp:
            continue
        total = sum(p["views"] for p in lp)
        t0 = _ts(lp[0]["t"])
        acc, half_t = 0, None
        for p in lp:
            acc += p["views"]
            if acc >= total / 2:
                half_t = _ts(p["t"]); break
        hours: dict[int, int] = defaultdict(int)
        for p in lp:
            hours[int((_ts(p["t"]) - t0) // 3600)] += p["views"]
        peak_h = max(hours.items(), key=lambda kv: kv[1])[0] if hours else 0
        pb = sorted(aimed.get(l["key"], []), key=lambda p: p["t"])
        pb_first = _ts(pb[0]["t"]) if pb else None
        pb_views = sum(p["views"] for p in pb)
        rows.append({
            "lane": l["key"], "label": l["label"], "color": l["color"], "posts": len(lp), "views": total,
            "first": lp[0]["t"], "first_handle": lp[0]["handle"], "peak_h": peak_h,
            "half_life_h": round((half_t - t0) / 3600, 1) if half_t else None,
            "last_h": round((_ts(lp[-1]["t"]) - t0) / 3600, 1),
            "pushback_posts": len(pb), "pushback_views": pb_views,
            "pushback_share": round(pb_views / total, 3) if total else 0,
            "pushback_lag_h": round((pb_first - t0) / 3600, 1) if pb_first else None,
            "pushback_first_handle": pb[0]["handle"] if pb else None,
        })
    rows.sort(key=lambda r: r["first"])
    return rows


def early_vs_late(rows: list[dict[str, Any]]) -> dict[str, Any]:
    early = [r for r in rows if r["pushback_lag_h"] is not None and r["pushback_lag_h"] <= EARLY_H]
    late = [r for r in rows if r not in early]
    def summ(g):
        hl = [r["half_life_h"] for r in g if r["half_life_h"] is not None]
        return {"lanes": len(g), "mean_half_life_h": round(sum(hl) / len(hl), 1) if hl else None,
                "mean_views": int(sum(r["views"] for r in g) / len(g)) if g else None,
                "mean_pushback_share": round(sum(r["pushback_share"] for r in g) / len(g), 3) if g else None}
    return {"early_h": EARLY_H, "early": summ(early), "late_or_none": summ(late)}


def analysis(inc: Incident) -> Path:
    data_path = inc.out_dir / "data.json"
    if not data_path.exists():
        raise SystemExit("no data.json yet — run export first")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = lane_timing(data)
    evl = early_vs_late(rows)
    markets = data.get("markets") or []
    ll = lead_lag(data["posts"], [l for l in data["lanes"] if l["key"] != PUSHBACK_KEY] + [l for l in data["lanes"] if l["key"] == PUSHBACK_KEY],
                  markets[0]) if markets else []
    from .events import response
    try:
        data_end = max(datetime.fromisoformat(p["t"]) for p in data["posts"] if p.get("t"))
    except ValueError:
        data_end = None
    ev_rows = response(data["posts"], data["lanes"], data.get("events") or [], data_end=data_end)
    out = {"lanes": rows, "early_vs_late": evl, "market": markets[0]["label"] if markets else None, "lead_lag": ll,
           "events": ev_rows, "generated_at": datetime.now(timezone.utc).isoformat()}
    (inc.out_dir / "analysis.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    page = render_html(inc, data, out)
    print(f"wrote {page}  ({len(rows)} lanes; early-pushback lanes={evl['early']['lanes']}, lead/lag rows={len(ll)})")
    for r in rows:
        print(f"  {r['label'][:34]:<34} first {r['first'][5:16]} half-life {str(r['half_life_h'])+'h':>7}  pushback lag {str(r['pushback_lag_h'])+'h' if r['pushback_lag_h'] is not None else 'none':>8}  share {r['pushback_share']:.0%}")
    return page


CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --surface-2:#1d1d21; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e;
          --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  * { box-sizing:border-box; } body { margin:0 auto; background:var(--bg); color:var(--ink); font-family:var(--sans); padding:16px 16px 48px; max-width:1100px; }
  h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:14px; margin:26px 0 8px; color:var(--ink-2); } .sub { color:var(--ink-3); font-size:11.5px; max-width:90ch; margin:0 0 12px; line-height:1.4; }
  .wrap { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:12.5px; min-width:760px; }
  th { text-align:left; color:var(--ink-3); font-weight:600; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; } td.n, th.n { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .lane { font-weight:600; } .muted { color:var(--ink-3); font-size:11px; } .tiles { display:flex; gap:14px; flex-wrap:wrap; }
  .tile { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:220px; }
  .tile .t { font-size:11px; color:var(--ink-3); text-transform:uppercase; letter-spacing:.05em; } .tile .v { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
  .tile .d { font-size:11.5px; color:var(--ink-2); margin-top:2px; } a.back { color:var(--ink-2); font-size:12px; } footer { margin-top:18px; color:var(--ink-3); font-size:11px; }
  .bar { display:inline-block; height:8px; background:var(--accent); border-radius:2px; vertical-align:middle; }
"""


def _fmt(n: float) -> str:
    return f"{n/1e6:.1f}M".replace(".0M", "M") if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else f"{int(n)}"


def render_html(inc: Incident, data: dict[str, Any], out: dict[str, Any]) -> Path:
    tz = ZoneInfo(inc.timezone)
    fmt = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")  # noqa: E731
    rows = out["lanes"]
    trs = "".join(
        f'<tr><td><span class="lane" style="color:{r["color"]}">{html.escape(r["label"])}</span><div class="muted">first: @{html.escape(r["first_handle"])}</div></td>'
        f'<td>{fmt(r["first"])}</td><td class="n">{r["posts"]}</td><td class="n">{_fmt(r["views"])}</td>'
        f'<td class="n">{r["half_life_h"] if r["half_life_h"] is not None else "—"}</td><td class="n">{r["peak_h"]}</td>'
        f'<td class="n">{r["pushback_lag_h"] if r["pushback_lag_h"] is not None else "none"}</td>'
        f'<td class="n">{r["pushback_posts"]}</td><td class="n">{r["pushback_share"]:.0%} <span class="bar" style="width:{min(100, r["pushback_share"]*200):.0f}px"></span></td>'
        f'<td class="muted">{("@" + html.escape(r["pushback_first_handle"])) if r["pushback_first_handle"] else ""}</td></tr>'
        for r in rows)
    e, l = out["early_vs_late"]["early"], out["early_vs_late"]["late_or_none"]
    tiles = f"""<div class="tiles">
<div class="tile"><div class="t">Pushback within {EARLY_H}h</div><div class="v">{e["lanes"]} lanes</div><div class="d">mean half-life {e["mean_half_life_h"] if e["mean_half_life_h"] is not None else "—"}h · mean views {_fmt(e["mean_views"]) if e["mean_views"] else "—"} · pushback share {f'{e["mean_pushback_share"]:.0%}' if e["mean_pushback_share"] is not None else "—"}</div></div>
<div class="tile"><div class="t">Pushback later or never</div><div class="v">{l["lanes"]} lanes</div><div class="d">mean half-life {l["mean_half_life_h"] if l["mean_half_life_h"] is not None else "—"}h · mean views {_fmt(l["mean_views"]) if l["mean_views"] else "—"} · pushback share {f'{l["mean_pushback_share"]:.0%}' if l["mean_pushback_share"] is not None else "—"}</div></div>
</div>"""
    ll = out["lead_lag"]
    ll_html = ""
    if ll:
        lrows = "".join(f'<tr><td><span class="lane">{html.escape(r["label"])}</span></td><td class="n">{_fmt(r["views"])}</td><td class="n">{r["lag_h"]:+d}h</td><td class="n">{r["r"]:+.2f}</td><td class="muted">{r["direction"]}</td></tr>' for r in ll)
        ll_html = f"""<h2>Lane velocity vs. {html.escape(out["market"])}</h2>
<p class="sub">Correlation between each lane's hourly views (log) and the market's hourly price change, at the lag where it is strongest (±24h). A positive lag means the narrative moved before the price; |r| below about 0.2 is noise. Descriptive, not causal — one week of hourly data.</p>
<div class="wrap"><table><thead><tr><th>Lane</th><th class="n">Views</th><th class="n">Best lag</th><th class="n">r</th><th>Reading</th></tr></thead><tbody>{lrows}</tbody></table></div>"""
    ev_html = ""
    if out.get("events"):
        from .events import AFTER_H
        lanes_all = data["lanes"]
        head = "".join(f'<th class="n" title="{html.escape(l["label"])}"><span style="color:{l["color"]}">{html.escape(l["label"][:18])}</span></th>' for l in lanes_all)
        etrs = []
        for e in out["events"]:
            cells = []
            for l in lanes_all:
                c = e["lanes"].get(l["key"]) or {}
                r = c.get("ratio")
                if c.get("after_views", 0) == 0 and c.get("before_views", 0) == 0:
                    cells.append('<td class="n muted">—</td>')
                else:
                    txt = "new" if r is None or r == float("inf") else f"{r:.1f}×"
                    tone = "#ff8a80" if (r is None or r == float("inf") or r >= 3) and c.get("after_views", 0) >= 50_000 else "#f0b06a" if (r or 0) >= 1.5 else "var(--ink-2)"
                    cells.append(f'<td class="n" title="after {_fmt(c["after_views"])} views / {c["after_posts"]} posts; before {_fmt(c["before_views"])} / {c["before_posts"]}"><span style="color:{tone}">{txt}</span><div class="muted">{_fmt(c["after_views"])}</div></td>')
            tot = e["ratio"]
            tot_txt = "new" if tot is None or tot == float("inf") else f"{tot:.1f}×"
            when = (datetime.fromisoformat(e["at"]).astimezone(tz).strftime("%a %b %d").replace(" 0", " ") + " (all day)") if e.get("all_day") else fmt(e["at"])
            top = next((l["label"] for l in lanes_all if l["key"] == e.get("top_lane")), "")
            etrs.append(f'<tr><td><b>{html.escape(e["label"])}</b><div class="muted">{html.escape(e["type"])} · {when}{(" · " + html.escape(e["source"])) if e.get("source") else ""}</div></td>'
                        f'<td class="n">{tot_txt}<div class="muted">{_fmt(e["after_views"])}</div></td>{"".join(cells)}<td class="muted">{html.escape(top)}</td></tr>')
        ev_html = f"""<h2>Events and how the lanes answered</h2>
<p class="sub">From <code>incidents/events.yaml</code> and poll releases found in the claims (labelled "from N claims"): for each off-platform event, views posted in the {AFTER_H} hours after it versus the {AFTER_H} hours before, per lane
(an all-day event compares its day with the day before). "new" means the lane had nothing in the hours before. Times taken from the first post on X are a lower bound for the moment itself.
Red: three times the views or more, at 50K+ views after.</p>
<div class="wrap"><table><thead><tr><th style="min-width:230px">Event</th><th class="n">All lanes</th>{head}<th>Biggest lane after</th></tr></thead><tbody>{"".join(etrs)}</tbody></table></div>"""
    rings_html = ""
    rings_path = inc.out_dir / "rings.json"
    if rings_path.exists():
        rj = json.loads(rings_path.read_text(encoding="utf-8"))
        rr = rj.get("rings") or []
        if rr:
            lane_label = {l["key"]: l["label"] for l in data["lanes"]}
            rtrs = []
            for r in rr[:25]:
                tone = "#ff8a80" if r.get("suspicious") else "var(--ink-2)"
                lanes_txt = ", ".join(f"{html.escape(lane_label.get(k, k))} {n}" for k, n in list(r["lanes"].items())[:2]) or "—"
                rtrs.append(f'<tr><td><a href="https://{html.escape(r["url"])}" style="color:var(--ink)">{html.escape(r["url"][:70])}</a>'
                            f'<div class="muted">{html.escape(r["sample"][:140])}</div></td>'
                            f'<td class="n"><b style="color:{tone}">{r["burst_accounts"]}</b> / {r["accounts"]}</td><td>{fmt(r["burst_start"])}</td>'
                            f'<td class="n">{r["posts"]}</td><td class="n">{_fmt(r["views"])}</td><td class="n">{r["new_accounts"]:.0%}</td><td class="n">{r.get("small_accounts", 0):.0%}</td>'
                            f'<td class="muted">{lanes_txt}</td><td class="muted">{html.escape(", ".join("@" + h for h in r["handles"][:5]))}</td></tr>')
            rings_html = f"""<h2>Link rings</h2>
<p class="sub">The same link shared by {rj["min_accounts"]}+ distinct accounts inside {rj["window_hours"]:g} hours. Reporters and poll accounts do this within the hour of a story
and that is news; a ring where a fifth or more of the accounts are under 90 days old, or most have under 5K followers, is marked red and counts toward the control score.
"Burst / all" is accounts inside the tightest window over all accounts that ever shared it.</p>
<div class="wrap"><table><thead><tr><th>Link</th><th class="n">Burst / all</th><th>Window start</th><th class="n">Posts</th><th class="n">Views</th><th class="n">New accts</th><th class="n">Small accts</th><th>Lanes</th><th>In the burst</th></tr></thead><tbody>{"".join(rtrs)}</tbody></table></div>"""
    ctrl_html = ""
    ctrl_path = inc.out_dir / "control.json"
    if ctrl_path.exists():
        from .control import FLAG, NEW_ACCOUNT_DAYS, table_html
        ctrl = json.loads(ctrl_path.read_text(encoding="utf-8"))
        claims_part = ""
        if ctrl.get("claims"):
            claims_part = f"""<h2>Canonical claims that look pushed</h2>
<p class="sub">Same features, per canonical claim with five or more posts (from <a href="claims.html" style="color:var(--ink-2)">what is claimed</a>). A poll release spreads in a burst with many copies on its own; a claim that scores high without an event behind it is the one to look at.</p>
{table_html(ctrl["claims"][:15], with_color=False)}"""
        ctrl_html = f"""<h2>Narrative control</h2>
<p class="sub">How much each lane looks like a push rather than organic spread, 0-100: copies (share of posts repeating another account's words),
new accounts (posters under {NEW_ACCOUNT_DAYS} days old), burst (share of posts in the single busiest hour), regularity (copies spaced on a clock, 0-1),
top-5 share (views carried by the five biggest accounts) and link rings (share of posts pushing one link alongside new or small accounts).
{FLAG}+ is flagged on the hub as a coordinated push. The features matter more than the number.</p>
{table_html(ctrl["lanes"])}
{claims_part}"""
    from .officials import said, table_html as officials_table
    said_rows = said(inc.slug, days=14)
    officials_html = f"""<h2>What electeds said</h2>
<p class="sub">Posts in this tracker by elected officials, candidates, agencies and public bodies (<code>incidents/officials.yaml</code>), last 14 days, newest first — the rhetoric the lanes answer or amplify. {len(said_rows)} posts.</p>
{officials_table(said_rows, inc.timezone)}"""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(inc.title)} — analysis</title><style>{CSS}</style></head><body>
<a class="back" href="./">← chart</a> · <a class="back" href="accounts.html">who moved it</a>
<h1>Timing and pushback: {html.escape(inc.title)}</h1>
<p class="sub">For each narrative lane: when it started, how fast it burned (half-life = hours until half of its eventual views had posted), when the first pushback aimed at it landed (a pushback post quoting or replying to a post in the lane), and how much pushback it drew relative to its own views.</p>
{tiles}
{officials_html}
<h2>Lanes</h2>
<div class="wrap"><table><thead><tr><th>Lane</th><th>First post</th><th class="n">Posts</th><th class="n">Views</th><th class="n">Half-life h</th><th class="n">Peak hour</th><th class="n">Pushback lag h</th><th class="n">Pushback posts</th><th class="n">Pushback share</th><th>First pushback</th></tr></thead><tbody>{trs}</tbody></table></div>
{ev_html}
{ll_html}
{rings_html}
{ctrl_html}
<footer>Generated {datetime.now(tz).strftime("%b %d, %Y %I:%M %p %Z").replace(" 0", " ")}. Small-N: treat group means as description of this incident, not a law.</footer>
</body></html>"""
    page = inc.out_dir / "analysis.html"
    page.write_text(doc, encoding="utf-8")
    return page
