"""Build site/index.html — a hub listing every rendered tracker with its headline numbers."""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import OUT_DIR, ROOT

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Texas Narrative Trackers</title>
<style>
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e; }
  * { box-sizing: border-box; } body { margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; padding:24px 16px 48px; max-width:960px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 4px; } .lead { color:var(--ink-3); font-size:13px; margin:0 0 20px; max-width:70ch; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }
  a.card { display:block; background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; color:inherit; text-decoration:none; }
  a.card:hover { border-color:#4a4a52; } .card h2 { font-size:15px; margin:0 0 6px; } .card .w { color:var(--ink-3); font-size:11.5px; margin-bottom:10px; }
  .nums { display:flex; gap:16px; } .n { font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; } .n small { display:block; font-size:10.5px; font-weight:400; color:var(--ink-3); }
  .n.push { color:var(--accent); } .n.back { color:#2fbf8c; } .live { display:inline-block; font-size:10px; letter-spacing:.06em; text-transform:uppercase; color:#2fbf8c; border:1px solid #2fbf8c55; border-radius:999px; padding:1px 7px; margin-left:6px; vertical-align:middle; }
  footer { margin-top:24px; color:var(--ink-3); font-size:11px; }
</style></head><body>
<h1>Texas Narrative Trackers</h1>
<p class="lead">How political stories spread on X: every post with 1K+ views, sorted into the narrative it pushes, linked to what it quoted or copied. Live trackers update every six hours.</p>
@@ALERTS@@
<div class="grid">@@CARDS@@</div>
<p class="lead" style="margin-top:18px"><a href="accounts/" style="color:#8cf">Amplifier map →</a> the accounts that move these narratives, across every tracker.</p>
<footer>Built @@BUILT@@. Data via twitterapi.io; lanes assigned by Claude.</footer>
</body></html>"""

CARD = """<a class="card" href="{slug}/"><h2>{title}{live}</h2><div class="w">{window}{updated}</div>
<div class="nums"><div class="n push">{pushing}<small>pushing the story</small></div><div class="n">{repeated}<small>repeated</small></div><div class="n back">{pushback}<small>pushing back</small></div></div></a>"""


def build(site_dir: Path, tz: str = "America/Chicago") -> Path:
    cards = []
    for d in sorted(OUT_DIR.iterdir()):
        if d.name == "accounts":
            continue
        data_path = d / "data.json"
        if not (d / "index.html").exists() or not data_path.exists():
            continue
        data = json.loads(data_path.read_text(encoding="utf-8"))
        meta, stats = data.get("meta", {}), data.get("stats", {})
        if meta.get("synthetic"):
            continue
        live = (ROOT / "incidents" / f"{d.name}.yaml").exists() and "end: today" in (ROOT / "incidents" / f"{d.name}.yaml").read_text(encoding="utf-8")
        w = meta.get("window", {})
        updated = ""
        if meta.get("pulled_at"):
            try:
                dt = datetime.fromisoformat(meta["pulled_at"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
                updated = f" · updated {dt.strftime('%b %d, %I:%M %p').replace(' 0', ' ')}"
            except ValueError:
                pass
        cards.append(CARD.format(slug=d.name, title=html.escape(meta.get("title", d.name)),
                                 live='<span class="live">live</span>' if live else "",
                                 window=f"{w.get('start', '')} → {w.get('end', '')}", updated=updated,
                                 pushing=stats.get("pushing", 0), repeated=stats.get("repeated", 0), pushback=stats.get("pushback", 0)))
    # live trackers first
    cards.sort(key=lambda c: ('class="live"' not in c, c))
    alerts_html = ""
    items = []
    for d in sorted(OUT_DIR.iterdir()):
        ap = d / "alerts.json"
        if not ap.exists():
            continue
        try:
            a = json.loads(ap.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for al in a.get("alerts", []):
            if al["type"] == "new_cluster":
                items.append(f'<li><b>{html.escape(al["title"])}</b> <span style="color:#7c7c78">in {html.escape(d.name)} · {al["posts"]} posts · {al["views"]:,} views · {al["accounts"]} accounts</span><br><span style="color:#b9b9b3;font-size:12px">@{html.escape(al["samples"][0]["handle"])}: {html.escape(al["samples"][0]["text"][:160])}</span></li>')
            else:
                items.append(f'<li><b>{html.escape(al["title"])}</b> surging <span style="color:#7c7c78">in {html.escape(d.name)} · {al["views_6h"]:,} views in 6h ({al["ratio"]}x normal)</span></li>')
    if items:
        alerts_html = '<h2 style="font-size:15px;margin:22px 0 6px;color:#e08a2e">Forming now</h2><ul style="font-size:13px;line-height:1.5;padding-left:18px">' + "".join(items) + "</ul>"
    # coordinated pushes: lanes or canonical claims whose control score crossed the flag line (tracker.control)
    pushes = []
    for d in sorted(OUT_DIR.iterdir()):
        cp = d / "control.json"
        if not cp.exists():
            continue
        try:
            c = json.loads(cp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for r in c.get("flagged", [])[:3]:
            page = "analysis.html" if r["kind"] == "lane" else "claims.html"
            pushes.append(f'<li><b>{html.escape(r["label"])}</b> <span style="color:#7c7c78">{r["kind"]} in <a href="{html.escape(d.name)}/{page}" style="color:#8cf">{html.escape(d.name)}</a> · control {r["score"]} · {r["posts"]} posts · {r["accounts"]} accounts</span><br><span style="color:#b9b9b3;font-size:12px">{html.escape("; ".join(r["why"]))}</span></li>')
    # gaining now: claims with the most momentum over the last six hours (tracker.features heuristic, until the model exists)
    gaining = []
    for d in sorted(OUT_DIR.iterdir()):
        fp = d / "features.json"
        if not fp.exists():
            continue
        try:
            fj = json.loads(fp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for r in fj.get("likely_to_grow", [])[:3]:
            if r["accounts"] >= 3 and r["views_6h"] >= 20_000:
                gaining.append((r["heuristic"], d.name, r))
    gaining.sort(key=lambda g: -g[0])
    if gaining:
        lis = "".join(f'<li><b>{html.escape(r["label"])}</b> <span style="color:#7c7c78">in <a href="{html.escape(slug)}/claims.html" style="color:#8cf">{html.escape(slug)}</a> · {r["views_6h"]:,} views in 6h'
                      f'{" · " + str(r["momentum_6h"]) + "× the prior 6h" if r["momentum_6h"] not in (None, 99.0) else " · new"} · {r["accounts"]} accounts{" · after a calendar event" if r["event_follow"] else ""}</span></li>'
                      for _, slug, r in gaining[:6])
        alerts_html += '<h2 style="font-size:15px;margin:22px 0 6px;color:#8fb4ff">Gaining now</h2><p class="lead" style="margin:0 0 6px">Claims with the most momentum over the last six hours of data, from more than one account.</p><ul style="font-size:13px;line-height:1.5;padding-left:18px">' + lis + "</ul>"
    if pushes:
        alerts_html += '<h2 style="font-size:15px;margin:22px 0 6px;color:#ff5252">Coordinated pushes</h2><p class="lead" style="margin:0 0 6px">Lanes and claims whose spread looks engineered: copies across accounts, new accounts, bursts, clockwork timing.</p><ul style="font-size:13px;line-height:1.5;padding-left:18px">' + "".join(pushes) + "</ul>"
    # run health strip from status.json (written by tracker.status just before the hub)
    status_html = ""
    sp = site_dir / "status.json"
    if sp.exists():
        try:
            stt = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            stt = {}
        b = stt.get("balance") or {}
        pills = []
        if "usd" in b:
            col = "#e66767" if b.get("low") else "#2fbf8c"
            pills.append(f'<span style="color:{col}">● twitterapi.io ${b["usd"]:.2f}{" — recharge" if b.get("low") else ""}{" · free tier (5s pacing)" if b.get("free_tier") else ""}</span>')
        elif b.get("error"):
            pills.append(f'<span style="color:#7c7c78">● balance unavailable ({html.escape(str(b["error"]))})</span>')
        try:
            g = datetime.fromisoformat(stt["generated_at"]).astimezone(ZoneInfo(tz))
            pills.append(f'<span>last run {g.strftime("%b %d, %I:%M %p").replace(" 0", " ")}</span>')
        except (KeyError, ValueError):
            pass
        bad = []
        for t in stt.get("trackers", []):
            if not t.get("exists") or not t.get("live"):
                continue
            flags = []
            if t.get("lanes", 0) == 0: flags.append("no lanes yet")
            if t.get("pull_partial"): flags.append("pull in progress")
            if t.get("unlabelled_1k", 0) > 0: flags.append(f'{t["unlabelled_1k"]} unlabelled')
            if t.get("errors"): flags.append(f'{t["errors"]} query errors')
            if flags:
                bad.append(f'{html.escape(t["slug"])}: {", ".join(flags)}')
        if bad:
            pills.append('<span style="color:#c98500">' + " · ".join(bad) + "</span>")
        if stt.get("run_url"):
            pills.append(f'<a href="{html.escape(stt["run_url"])}" style="color:#8cf">run log</a>')
        if pills:
            status_html = '<p style="font-size:11.5px;color:#b9b9b3;margin:-8px 0 16px;display:flex;gap:14px;flex-wrap:wrap">' + "".join(f"<span>{x}</span>" for x in pills) + "</p>"
    site_dir.mkdir(parents=True, exist_ok=True)
    out = site_dir / "index.html"
    built = datetime.now(ZoneInfo(tz)).strftime("%b %d, %Y %I:%M %p %Z").replace(" 0", " ")
    out.write_text(PAGE.replace("@@CARDS@@", "\n".join(cards)).replace("@@BUILT@@", built).replace("@@ALERTS@@", status_html + alerts_html))
    print(f"wrote {out} ({len(cards)} trackers)")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site")
