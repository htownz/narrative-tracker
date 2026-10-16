"""Lane review: sample posts per lane so the labels can be spot-checked, and an overrides file that sticks.

    python run.py review incidents/x.yaml            -> out/<slug>/review.html
    incidents/<slug>.overrides.csv                   -> post_id,lane   (applied after every classify)

The review page shows up to `per_lane` posts for each lane (the most-viewed, plus a random slice so the
tail is represented). Each row has a lane dropdown; changing one adds a line to the overrides box at the
top, which you paste into incidents/<slug>.overrides.csv. `classify` applies that file last, so a
correction survives re-labelling and live refreshes.
"""
from __future__ import annotations

import csv
import html
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import OTHER_KEY, PUSHBACK_KEY, ROOT, Incident
from .store import Store, profile_url


def overrides_path(inc: Incident) -> Path:
    return ROOT / "incidents" / f"{inc.slug}.overrides.csv"


def load_overrides(inc: Incident) -> dict[str, str]:
    p = overrides_path(inc)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    with p.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row or row[0].strip().startswith("#") or row[0].strip().lower() == "post_id":
                continue
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                out[row[0].strip()] = row[1].strip()
    return out


def apply_overrides(inc: Incident, store: Store | None = None) -> int:
    """Set lane = override for every listed post. Unknown lane keys are ignored (reported)."""
    ov = load_overrides(inc)
    if not ov:
        return 0
    valid = {l["key"] for l in inc.lane_table()} | {OTHER_KEY}
    own = store is None
    store = store or Store(inc.db_path)
    good = {pid: lane for pid, lane in ov.items() if lane in valid}
    bad = sorted(set(ov.values()) - valid)
    if bad:
        print(f"  overrides: ignoring unknown lane keys {bad}")
    store.set_lanes(good, "manual")
    if own:
        store.close()
    print(f"  overrides applied: {len(good)} posts")
    return len(good)


def sample(inc: Incident, per_lane: int = 20, seed: int = 1) -> dict[str, list[dict[str, Any]]]:
    store = Store(inc.db_path)
    posts = [p for p in store.all_posts() if p["created_at"] and int(p["view_count"] or 0) >= inc.min_views and p["lane"]]
    store.close()
    rng = random.Random(seed)
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in posts:
        by_lane[p["lane"]].append(p)
    out: dict[str, list[dict[str, Any]]] = {}
    for lane, lp in by_lane.items():
        lp.sort(key=lambda p: -int(p["view_count"] or 0))
        top = lp[: per_lane // 2]
        rest = lp[per_lane // 2:]
        tail = rng.sample(rest, min(len(rest), per_lane - len(top))) if rest else []
        out[lane] = top + sorted(tail, key=lambda p: -int(p["view_count"] or 0))
    return out


CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --surface-2:#1d1d21; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e;
          --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  * { box-sizing:border-box; } body { margin:0 auto; background:var(--bg); color:var(--ink); font-family:var(--sans); padding:16px 16px 48px; max-width:1100px; }
  h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:28px 0 8px; padding-top:10px; border-top:1px solid var(--line); }
  .sub { color:var(--ink-3); font-size:11.5px; max-width:95ch; margin:0 0 12px; line-height:1.45; } a.back { color:var(--ink-2); font-size:12px; }
  .box { position:sticky; top:0; z-index:5; background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin:10px 0 16px; }
  .box .t { font-size:11px; color:var(--ink-3); text-transform:uppercase; letter-spacing:.05em; display:flex; justify-content:space-between; align-items:center; }
  .box textarea { width:100%; min-height:54px; background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:6px; font-family:var(--mono); font-size:11.5px; padding:6px 8px; margin-top:6px; resize:vertical; }
  .box button { background:var(--surface-2); color:var(--ink); border:1px solid var(--line); border-radius:999px; padding:4px 10px; font-size:11.5px; cursor:pointer; }
  .post { display:grid; grid-template-columns: 150px 1fr 150px; gap:10px; padding:8px 0; border-bottom:1px solid var(--line); align-items:start; font-size:12.5px; }
  .who { font-family:var(--mono); font-size:11px; color:var(--ink-2); word-break:break-all; } .who b { color:var(--ink); } .who a { color:inherit; text-decoration:none; }
  .txt { white-space:pre-wrap; line-height:1.4; } .txt a { color:var(--ink-3); font-size:11px; text-decoration:none; margin-left:6px; }
  select { width:100%; background:var(--surface-2); color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:5px 6px; font-size:11.5px; }
  select.changed { border-color:var(--accent); color:var(--accent); } .meta { color:var(--ink-3); font-size:10.5px; margin-top:3px; }
  .lanehead { display:flex; align-items:baseline; gap:10px; } .lanehead .d { color:var(--ink-3); font-size:11.5px; font-weight:400; }
  @media (max-width:640px) { .post { grid-template-columns: 1fr; } }
"""

SCRIPT = """
<script>
  const box = document.getElementById('ov'); const sels = document.querySelectorAll('select[data-id]'); const count = document.getElementById('cnt');
  const lines = new Map();
  function refresh() { box.value = [...lines.entries()].map(([id, l]) => id + ',' + l).join('\\n'); count.textContent = lines.size + ' change' + (lines.size === 1 ? '' : 's'); }
  sels.forEach(s => s.addEventListener('change', () => { const same = s.value === s.dataset.orig; s.classList.toggle('changed', !same);
    if (same) lines.delete(s.dataset.id); else lines.set(s.dataset.id, s.value); refresh(); }));
  document.getElementById('copy').addEventListener('click', async () => { try { await navigator.clipboard.writeText(box.value); document.getElementById('copy').textContent = 'copied'; } catch (e) { box.select(); } });
</script>"""


def render_html(inc: Incident, samples: dict[str, list[dict[str, Any]]], existing: dict[str, str]) -> Path:
    tz = ZoneInfo(inc.timezone)
    lanes = inc.lane_table()
    labels = {l["key"]: l["label"] for l in lanes} | {OTHER_KEY: "other (not drawn)"}
    colors = {l["key"]: l["color"] for l in lanes} | {OTHER_KEY: "#7c7c78"}
    order = [l["key"] for l in lanes] + [OTHER_KEY]
    options = "".join(f'<option value="{k}">{html.escape(labels[k])}</option>' for k in order)
    sections = []
    for key in order:
        lp = samples.get(key, [])
        if not lp:
            continue
        l = next((x for x in lanes if x["key"] == key), {"label": labels[key], "description": ""})
        rows = []
        for p in lp:
            t = datetime.fromisoformat(p["created_at"]).astimezone(tz).strftime("%b %d, %I:%M %p").replace(" 0", " ")
            src = p.get("lane_source") or ""
            link = f'<a href="{html.escape(p["url"])}" target="_blank" rel="noopener">open ↗</a>' if p.get("url") else ""
            manual = '<br><span style="color:var(--accent)">manual</span>' if src == "manual" else ""
            opts = options.replace(f'value="{key}"', f'value="{key}" selected', 1)
            rows.append(
                '<div class="post"><div class="who"><b><a href="' + html.escape(profile_url(p["author_handle"])) + '" target="_blank" rel="noopener">@'
                + html.escape(p["author_handle"]) + '</a></b><br>' + f'{int(p["view_count"] or 0):,}' + ' views<br>' + t + manual + '</div>'
                + '<div class="txt">' + html.escape(p["text"] or "") + link + '</div>'
                + '<div><select data-id="' + p["id"] + '" data-orig="' + key + '">' + opts + '</select><div class="meta">'
                + ("in dataset" if p["in_dataset"] else "quoted post") + '</div></div></div>')
        sections.append(f'<h2 class="lanehead"><span style="color:{colors[key]}">{html.escape(labels[key])}</span><span class="d">{html.escape(l.get("description", ""))}</span></h2>' + "".join(rows))
    n_existing = len(existing)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(inc.title)} — lane review</title><style>{CSS}</style></head><body>
<a class="back" href="./">← chart</a> · <a class="back" href="accounts.html">who moved it</a> · <a class="back" href="analysis.html">timing</a>
<h1>Lane review: {html.escape(inc.title)}</h1>
<p class="sub">Up to 20 posts per lane: the ten most viewed plus ten drawn at random from the rest. Change a dropdown where the lane is wrong; the lines below are what to paste into <code>incidents/{html.escape(inc.slug)}.overrides.csv</code> (one <code>post_id,lane</code> per line). The job applies that file after every classification, so corrections stick. {n_existing} override{"s" if n_existing != 1 else ""} currently on file.</p>
<div class="box"><div class="t"><span>overrides to paste · <span id="cnt">0 changes</span></span><button id="copy">copy</button></div><textarea id="ov" spellcheck="false" placeholder="post_id,lane"></textarea></div>
{"".join(sections)}
<p class="sub" style="margin-top:20px">Generated {datetime.now(tz).strftime("%b %d, %Y %I:%M %p %Z").replace(" 0", " ")}.</p>
{SCRIPT}</body></html>"""
    out = inc.out_dir / "review.html"
    out.write_text(doc, encoding="utf-8")
    return out


def review(inc: Incident, per_lane: int = 20) -> Path:
    samples = sample(inc, per_lane=per_lane)
    out = render_html(inc, samples, load_overrides(inc))
    print(f"wrote {out}  ({sum(len(v) for v in samples.values())} sample posts across {len(samples)} lanes)")
    return out
