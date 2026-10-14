"""Amplifier map: who originates, who lifts, who copies, who pushes back.

    python run.py accounts incidents/x.yaml        -> out/<slug>/accounts.json + accounts.html
    python run.py accounts --all                   -> out/accounts/index.html (every store, aggregated by handle)

Roles (per account, per incident), from the stored posts and edges:
  originator  — wrote the first post in a same-words cluster, or a post that >= 2 others quoted/replied to
  bridge      — quoted/replied to an earlier post and took it to >= 3x its views (and >= 20K views)
  copier      — half or more of their posts repeat someone else's words
  pushback    — >= 60% of their posts are in the pushback lane
  poster      — everyone else
Reach = views of their own posts + views of the posts that quoted, replied to or copied them.
First mover = among the first five posts in a lane.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import OTHER_KEY, PUSHBACK_KEY, ROOT, OUT_DIR, Incident, load_incident
from .store import Store, parse_created_at

ROLE_ORDER = ["originator", "bridge", "pushback", "copier", "poster"]
ROLE_COLOR = {"originator": "#e08a2e", "bridge": "#3987e5", "copier": "#ffffff", "pushback": "#2fbf8c", "poster": "#7c7c78"}
BRIDGE_LIFT, BRIDGE_MIN_VIEWS, FIRST_MOVER_N = 3.0, 10_000, 5


def _account_age_years(raw: str | None) -> float | None:
    try:
        created = (json.loads(raw or "{}").get("author") or {}).get("createdAt")
        iso = parse_created_at(created)
        if not iso:
            return None
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days / 365.25, 1)
    except Exception:
        return None


def account_table(inc: Incident) -> list[dict[str, Any]]:
    store = Store(inc.db_path)
    lane_keys = {l["key"] for l in inc.lane_table()}
    posts = [p for p in store.all_posts()
             if p["created_at"] and int(p["view_count"] or 0) >= inc.min_views and p["lane"] in lane_keys]
    store.close()
    by_id = {p["id"]: p for p in posts}

    # edges: child -> parent (quote / reply), copy -> origin
    children: dict[str, list[dict]] = defaultdict(list)
    copies_of: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        parent = by_id.get(p["quoted_id"] or "") or by_id.get(p["in_reply_to_id"] or "")
        p["_parent"] = parent
        if parent:
            children[parent["id"]].append(p)
        if p["copy_origin"] and p["copy_origin"] in by_id:
            copies_of[p["copy_origin"]].append(p)

    # first movers per lane
    first_in_lane: dict[str, list[str]] = {}
    for lane in lane_keys:
        lane_posts = sorted((p for p in posts if p["lane"] == lane), key=lambda p: p["created_at"])
        first_in_lane[lane] = [p["author_handle"] for p in lane_posts[:FIRST_MOVER_N]]

    acc: dict[str, dict[str, Any]] = {}
    for p in posts:
        h = p["author_handle"]
        a = acc.setdefault(h, {
            "handle": h, "name": p["author_name"], "avatar": p["author_avatar"], "followers": int(p["author_followers"] or 0),
            "account_age_years": _account_age_years(p["raw"]),
            "posts": 0, "views": 0, "downstream_views": 0, "lanes": defaultdict(int),
            "originated": 0, "bridged": 0, "copied": 0, "max_lift": 0.0, "first_mover": set(), "top_post": None,
        })
        a["posts"] += 1
        a["views"] += int(p["view_count"] or 0)
        a["lanes"][p["lane"]] += 1
        kids, cps = children.get(p["id"], []), copies_of.get(p["id"], [])
        a["downstream_views"] += sum(int(k["view_count"] or 0) for k in kids) + sum(int(c["view_count"] or 0) for c in cps)
        if cps or len(kids) >= 2:
            a["originated"] += 1
        if p["copy_origin"]:
            a["copied"] += 1
        par = p["_parent"]
        if par:
            lift = int(p["view_count"] or 0) / max(1, int(par["view_count"] or 0))
            a["max_lift"] = max(a["max_lift"], lift)
            if lift >= BRIDGE_LIFT and int(p["view_count"] or 0) >= BRIDGE_MIN_VIEWS and par["author_handle"] != h:
                a["bridged"] += 1
        for lane, handles in first_in_lane.items():
            if h in handles and p["lane"] == lane:
                a["first_mover"].add(lane)
        if a["top_post"] is None or int(p["view_count"] or 0) > a["top_post"]["views"]:
            a["top_post"] = {"id": p["id"], "views": int(p["view_count"] or 0), "lane": p["lane"],
                             "text": (p["text"] or "")[:200], "url": p["url"], "t": p["created_at"]}

    rows = []
    for a in acc.values():
        n = a["posts"]
        pb = a["lanes"].get(PUSHBACK_KEY, 0) / n
        if pb >= 0.6:
            role = "pushback"
        elif a["originated"] and a["originated"] >= a["bridged"]:
            role = "originator"
        elif a["bridged"]:
            role = "bridge"
        elif a["copied"] / n >= 0.5:
            role = "copier"
        else:
            role = "poster"
        a["role"] = role
        a["reach"] = a["views"] + a["downstream_views"]
        a["pushback_share"] = round(pb, 2)
        a["lanes"] = dict(a["lanes"])
        a["first_mover"] = sorted(a["first_mover"])
        a["max_lift"] = round(a["max_lift"], 1)
        rows.append(a)
    rows.sort(key=lambda r: -r["reach"])
    return rows


# ---------------------------------------------------------------------------- HTML
CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --surface-2:#1d1d21; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e;
          --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans); padding:16px 16px 48px; max-width:1200px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--ink-3); font-size:11.5px; max-width:90ch; margin:0 0 12px; line-height:1.4; }
  .legend { display:flex; gap:10px; flex-wrap:wrap; margin:0 0 12px; font-size:11.5px; color:var(--ink-2); }
  .legend button { background:var(--surface-2); color:var(--ink); border:1px solid var(--line); border-radius:999px; padding:4px 10px; font-size:11.5px; cursor:pointer; }
  .legend button.on { border-color:var(--accent); color:var(--accent); }
  .badge { display:inline-block; font-size:10px; letter-spacing:.05em; text-transform:uppercase; padding:2px 7px; border-radius:999px; border:1px solid; white-space:nowrap; }
  .wrap { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:12.5px; min-width:860px; }
  th { text-align:left; color:var(--ink-3); font-weight:600; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:8px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:middle; }
  td.n, th.n { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .who { display:flex; align-items:center; gap:8px; min-width:200px; } .who img { width:28px; height:28px; border-radius:50%; background:var(--surface-2); flex:none; }
  .who .nm { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:170px; } .who .h { color:var(--ink-3); font-size:11px; font-family:var(--mono); }
  .who a { color:inherit; text-decoration:none; } .who a:hover { text-decoration:underline; }
  .mix { display:flex; height:10px; width:140px; border-radius:3px; overflow:hidden; gap:1px; background:var(--surface-2); } .mix span { display:block; height:100%; }
  .fm { color:var(--ink-2); font-size:11px; max-width:180px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; } .tp { color:var(--ink-3); font-size:11px; max-width:280px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  tr.hidden { display:none; } .inc { color:var(--ink-3); font-size:11px; }
  footer { margin-top:16px; color:var(--ink-3); font-size:11px; } a.back { color:var(--ink-2); font-size:12px; }
"""

SCRIPT = """
<script>
  const btns = document.querySelectorAll('.legend button'); const rows = document.querySelectorAll('tbody tr');
  let active = 'all';
  btns.forEach(b => b.addEventListener('click', () => { active = b.dataset.role; btns.forEach(x => x.classList.toggle('on', x === b));
    rows.forEach(r => r.classList.toggle('hidden', active !== 'all' && r.dataset.role !== active)); }));
</script>"""


def _fmt(n: int | float) -> str:
    n = float(n)
    return f"{n/1e6:.1f}M".replace(".0M", "M") if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else f"{int(n)}"


def _badge(role: str) -> str:
    c = ROLE_COLOR[role]
    return f'<span class="badge" style="color:{c};border-color:{c}66">{role}</span>'


def _mix(lanes: dict[str, int], colors: dict[str, str], labels: dict[str, str]) -> str:
    total = max(1, sum(lanes.values()))
    parts = "".join(f'<span style="width:{100*v/total:.1f}%;background:{colors.get(k, "#555")}" title="{html.escape(labels.get(k, k))}: {v}"></span>'
                    for k, v in sorted(lanes.items(), key=lambda kv: -kv[1]))
    return f'<div class="mix">{parts}</div>'


def render_html(inc: Incident, rows: list[dict[str, Any]], top_n: int = 80) -> Path:
    lanes = inc.lane_table()
    colors = {l["key"]: l["color"] for l in lanes}
    labels = {l["key"]: l["label"] for l in lanes}
    counts = defaultdict(int)
    for r in rows:
        counts[r["role"]] += 1
    legend = '<button class="on" data-role="all">all</button>' + "".join(
        f'<button data-role="{r}"><span style="color:{ROLE_COLOR[r]}">●</span> {r} · {counts[r]}</button>' for r in ROLE_ORDER)
    trs = []
    for i, r in enumerate(rows[:top_n], 1):
        img = f'<img src="{html.escape(r["avatar"])}" alt="">' if r["avatar"] else '<img alt="">'
        fm = ", ".join(labels.get(k, k) for k in r["first_mover"])
        tp = r["top_post"] or {}
        trs.append(
            f'<tr data-role="{r["role"]}"><td class="n">{i}</td>'
            f'<td><div class="who">{img}<div><div class="nm"><a href="https://x.com/{html.escape(r["handle"])}" target="_blank" rel="noopener">{html.escape(r["name"] or r["handle"])}</a></div>'
            f'<div class="h">@{html.escape(r["handle"])} · {_fmt(r["followers"])} followers{(" · " + str(r["account_age_years"]) + "y") if r["account_age_years"] is not None else ""}</div></div></div></td>'
            f'<td>{_badge(r["role"])}</td><td class="n">{r["posts"]}</td><td class="n">{_fmt(r["views"])}</td><td class="n">{_fmt(r["downstream_views"])}</td>'
            f'<td class="n"><b>{_fmt(r["reach"])}</b></td><td>{_mix(r["lanes"], colors, labels)}</td>'
            f'<td class="n">{r["max_lift"] if r["max_lift"] >= 1 else ""}</td><td class="fm">{html.escape(fm)}</td>'
            f'<td class="tp" title="{html.escape(tp.get("text", ""))}">{html.escape(tp.get("text", ""))}</td></tr>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(inc.title)} — accounts</title><style>{CSS}</style></head><body>
<a class="back" href="./">← chart</a>
<h1>Who moved it: {html.escape(inc.title)}</h1>
<p class="sub">Every account with a post at {inc.min_views:,}+ views in a lane, ranked by reach (their own views plus the views of posts that quoted, replied to or copied them).
Originator: first post in a same-words cluster, or quoted by two or more. Bridge: quoted an earlier post and took it to 3x its views. Copier: half or more of their posts repeat someone else's words. Pushback: most of their posts argue against the story.
Lane mix is coloured by lane; "first in" lists lanes where they were among the first five posts.</p>
<div class="legend">{legend}</div>
<div class="wrap"><table><thead><tr><th class="n">#</th><th>Account</th><th>Role</th><th class="n">Posts</th><th class="n">Own views</th><th class="n">Downstream</th><th class="n">Reach</th><th>Lane mix</th><th class="n">Max lift</th><th>First in</th><th>Top post</th></tr></thead>
<tbody>{"".join(trs)}</tbody></table></div>
<footer>{len(rows)} accounts · {sum(r["posts"] for r in rows)} posts · built {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</footer>
{SCRIPT}</body></html>"""
    out = inc.out_dir / "accounts.html"
    out.write_text(doc, encoding="utf-8")
    return out


def accounts(inc: Incident) -> list[dict[str, Any]]:
    rows = account_table(inc)
    (inc.out_dir / "accounts.json").write_text(json.dumps(rows, ensure_ascii=False, default=list), encoding="utf-8")
    out = render_html(inc, rows)
    roles = defaultdict(int)
    for r in rows:
        roles[r["role"]] += 1
    print(f"wrote {out}  ({len(rows)} accounts: " + ", ".join(f"{k}={roles[k]}" for k in ROLE_ORDER if roles[k]) + ")")
    for r in rows[:8]:
        print(f"  {_fmt(r['reach']):>6}  {r['role']:<10} @{r['handle']:<20} posts={r['posts']:<3} first in: {', '.join(r['first_mover'])[:50]}")
    return rows


# ---------------------------------------------------------------------------- cross-incident
def accounts_all() -> Path:
    incs = []
    for y in sorted((ROOT / "incidents").glob("*.yaml")):
        inc = load_incident(y)
        if inc.db_path.exists() and inc.lanes:
            incs.append(inc)
    agg: dict[str, dict[str, Any]] = {}
    for inc in incs:
        for r in account_table(inc):
            a = agg.setdefault(r["handle"], {"handle": r["handle"], "name": r["name"], "avatar": r["avatar"], "followers": r["followers"],
                                              "account_age_years": r["account_age_years"], "reach": 0, "views": 0, "posts": 0,
                                              "incidents": {}, "roles": defaultdict(int), "first_mover_n": 0})
            a["reach"] += r["reach"]; a["views"] += r["views"]; a["posts"] += r["posts"]
            a["incidents"][inc.slug] = {"role": r["role"], "reach": r["reach"], "posts": r["posts"]}
            a["roles"][r["role"]] += 1
            a["first_mover_n"] += len(r["first_mover"])
    rows = sorted(agg.values(), key=lambda a: (-len(a["incidents"]), -a["reach"]))
    for a in rows:
        a["role"] = max(a["roles"].items(), key=lambda kv: (kv[1], -ROLE_ORDER.index(kv[0])))[0]
        a["roles"] = dict(a["roles"])
    out_dir = OUT_DIR / "accounts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "accounts.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    cols = "".join(f'<th>{html.escape(i.slug.replace("-live", "").replace("-oct-2026", ""))}</th>' for i in incs)
    trs = []
    for n, a in enumerate(rows[:120], 1):
        cells = ""
        for inc in incs:
            x = a["incidents"].get(inc.slug)
            cells += f'<td>{_badge(x["role"])}<div class="inc">{_fmt(x["reach"])}</div></td>' if x else "<td></td>"
        img = f'<img src="{html.escape(a["avatar"])}" alt="">' if a["avatar"] else '<img alt="">'
        trs.append(f'<tr data-role="{a["role"]}"><td class="n">{n}</td><td><div class="who">{img}<div><div class="nm"><a href="https://x.com/{html.escape(a["handle"])}" target="_blank" rel="noopener">{html.escape(a["name"] or a["handle"])}</a></div>'
                   f'<div class="h">@{html.escape(a["handle"])} · {_fmt(a["followers"])} followers</div></div></div></td>'
                   f'<td class="n">{len(a["incidents"])}</td><td class="n"><b>{_fmt(a["reach"])}</b></td><td class="n">{a["posts"]}</td><td class="n">{a["first_mover_n"]}</td>{cells}</tr>')
    counts = defaultdict(int)
    for a in rows:
        counts[a["role"]] += 1
    legend = '<button class="on" data-role="all">all</button>' + "".join(
        f'<button data-role="{r}"><span style="color:{ROLE_COLOR[r]}">●</span> {r} · {counts[r]}</button>' for r in ROLE_ORDER)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Texas X amplifier map</title><style>{CSS}</style></head><body>
<a class="back" href="../">← trackers</a>
<h1>Amplifier map — accounts that move Texas political narratives</h1>
<p class="sub">Every account with a 1K+ view post in any tracker, ranked by how many trackers they appear in, then by total reach. Role is their most common role across trackers; the per-tracker cells show role and reach there. "First-mover" counts lanes where they were among the first five posts.</p>
<div class="legend">{legend}</div>
<div class="wrap"><table><thead><tr><th class="n">#</th><th>Account</th><th class="n">Trackers</th><th class="n">Reach</th><th class="n">Posts</th><th class="n">First-mover</th>{cols}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>
<footer>{len(rows)} accounts across {len(incs)} trackers · built {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</footer>{SCRIPT}</body></html>"""
    out = out_dir / "index.html"
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}  ({len(rows)} accounts across {len(incs)} trackers)")
    return out
