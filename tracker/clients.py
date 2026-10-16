"""The client desk: incidents/clients.yaml rendered as a page — each client, the policy areas it cares about,
the trackers watching them, what is moving in those trackers right now, and the legislative hooks and voices
to watch. Rebuilt at assemble time from the trackers' own outputs (claims.json, features.json, control.json).

clients() -> out/clients/index.html (+ clients.json)
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import OUT_DIR, ROOT

CLIENTS_PATH = ROOT / "incidents" / "clients.yaml"
STATUS_ORDER = {"client": 0, "pro-bono": 1, "prospect": 2, "former": 3}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def registry() -> dict[str, Any]:
    if not CLIENTS_PATH.exists():
        return {"clients": [], "firm_practices": []}
    return yaml.safe_load(CLIENTS_PATH.read_text(encoding="utf-8")) or {"clients": []}


def tracker_pulse(slug: str, days: int = 7) -> dict[str, Any]:
    """What a tracker says right now: whether it exists and has data, views this week, top lane, biggest claims
    this week with verdicts, gaining claims, flagged pushes."""
    d = _load(OUT_DIR / slug / "data.json")
    if not d:
        return {"slug": slug, "exists": (ROOT / "incidents" / f"{slug}.yaml").exists(), "live": False}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    posts = [p for p in d.get("posts", []) if p.get("t", "") >= since]
    lanes = {l["key"]: l for l in d.get("lanes", [])}
    by_lane: dict[str, int] = {}
    for p in posts:
        by_lane[p["lane"]] = by_lane.get(p["lane"], 0) + int(p.get("views") or 0)
    top = sorted(by_lane.items(), key=lambda kv: -kv[1])[:3]
    cj = _load(OUT_DIR / slug / "claims.json") or {}
    claims = [c for c in cj.get("clusters", []) if c.get("last_seen", "") >= since]
    claims.sort(key=lambda c: -c.get("views", 0))
    fj = _load(OUT_DIR / slug / "features.json") or {}
    gaining = [g for g in fj.get("likely_to_grow", [])[:3] if g.get("accounts", 0) >= 2]
    ctl = _load(OUT_DIR / slug / "control.json") or {}
    from .officials import said
    electeds = said(slug, days)[:4]
    ph = _load(OUT_DIR / slug / "phrases.json") or {}
    return {
        "phrases": [{"phrase": r["phrase"], "posts": r["posts"], "articles": r["articles"], "accounts": r["accounts"]} for r in ph.get("phrases", []) if r["posts"] or r["articles"]][:4],
        "electeds": [{"name": r["name"], "office": r["office"], "lane": r["lane"], "views": r["views"], "text": r["text"], "url": r["url"]} for r in electeds],
        "slug": slug, "exists": True, "live": True, "title": d.get("meta", {}).get("title", slug),
        "posts_7d": len(posts), "views_7d": sum(int(p.get("views") or 0) for p in posts),
        "top_lanes": [{"label": lanes.get(k, {}).get("label", k), "color": lanes.get(k, {}).get("color", "#999"), "views": v} for k, v in top],
        "claims": [{"text": c["text"], "views": c["views"], "verdict": c.get("verdict"), "first_handle": c.get("first_handle"), "url": c.get("top_post")} for c in claims[:4]],
        "false_7d": sum(1 for c in claims if c.get("verdict") in ("unsupported", "misleading")),
        "gaining": [{"label": g["label"], "views_6h": g["views_6h"]} for g in gaining],
        "flagged": [{"label": f["label"], "score": f["score"]} for f in ctl.get("flagged", [])[:3]],
        "updated": d.get("meta", {}).get("generated_at", "")[:16],
    }


def build() -> dict[str, Any]:
    reg = registry()
    slugs = sorted({s for c in reg.get("clients", []) for a in c.get("areas", []) for s in a.get("trackers", [])})
    pulse = {s: tracker_pulse(s) for s in slugs}
    clients = sorted(reg.get("clients", []), key=lambda c: (STATUS_ORDER.get(c.get("status", "client"), 9), c.get("name", "")))
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "firm_practices": reg.get("firm_practices", []), "clients": clients, "trackers": pulse}
    (OUT_DIR / "clients").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "clients" / "clients.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


CSS = """
  :root { color-scheme: dark; --bg:#0b0b0c; --surface:#141416; --line:#2a2a30; --ink:#f2f2f0; --ink-2:#b9b9b3; --ink-3:#7c7c78; --accent:#e08a2e; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  * { box-sizing:border-box; } body { margin:0 auto; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; padding:16px 16px 48px; max-width:1100px; }
  h1 { font-size:20px; margin:0 0 4px; } .lead { color:var(--ink-3); font-size:12px; margin:0 0 18px; max-width:90ch; }
  .client { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 14px; }
  .client h2 { font-size:15px; margin:0; display:flex; gap:10px; align-items:baseline; } .status { font-size:10px; letter-spacing:.06em; text-transform:uppercase; color:var(--ink-3); border:1px solid var(--line); border-radius:999px; padding:1px 7px; }
  .status.client { color:#2fbf8c; border-color:#2fbf8c55; } .status.prospect { color:#8fb4ff; border-color:#8fb4ff55; } .status.pro-bono { color:var(--accent); border-color:#e08a2e55; }
  .meta { color:var(--ink-3); font-size:11px; margin:2px 0 8px; }
  .area { border-top:1px solid var(--line); padding:10px 0 2px; } .area h3 { font-size:13px; margin:0 0 4px; } .why { color:var(--ink-2); font-size:12px; margin:0 0 6px; max-width:100ch; }
  .row { display:grid; grid-template-columns: 1.3fr 1fr; gap:14px; } @media (max-width:760px) { .row { grid-template-columns:1fr; } }
  .tr { font-size:12px; } .tr a.t { color:var(--ink); font-weight:600; } .tr .n { font-family:var(--mono); color:var(--ink-3); font-size:11px; }
  .lane { display:inline-block; margin:2px 8px 2px 0; font-size:11px; } .lane i { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
  ul { margin:4px 0 6px; padding-left:16px; font-size:12px; line-height:1.45; } li .m { color:var(--ink-3); font-size:11px; }
  .pill { display:inline-block; font-size:10px; border-radius:999px; padding:0 6px; margin-left:4px; border:1px solid var(--line); color:var(--ink-3); } .pill.bad { color:#ff5252; border-color:#ff525255; }
  .hooks { font-size:11.5px; color:var(--ink-2); } .hooks b { color:var(--ink-3); font-weight:600; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; }
  .dead { color:var(--ink-3); font-size:12px; } .confirm { color:#e08a2e; font-size:12px; margin:0 0 6px; }
  footer { margin-top:22px; color:var(--ink-3); font-size:11px; }
"""


def _fmt(n: int | float) -> str:
    n = float(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else str(int(n))


def _tracker_html(p: dict[str, Any]) -> str:
    e = html.escape
    if not p.get("live"):
        return f'<div class="tr dead">{e(p["slug"])}: {"waiting for its first run" if p.get("exists") else "no such tracker yet"}</div>'
    lanes = "".join(f'<span class="lane"><i style="background:{l["color"]}"></i>{e(l["label"])} {_fmt(l["views"])}</span>' for l in p["top_lanes"])
    claims = "".join(f'<li><a href="{e(c["url"] or "#")}" style="color:var(--ink)">{e(c["text"][:140])}</a>'
                     f'<span class="m"> · {_fmt(c["views"])} views · @{e(c["first_handle"] or "")}</span>'
                     + (f'<span class="pill {"bad" if c["verdict"] in ("unsupported", "misleading") else ""}">{e(c["verdict"])}</span>' if c.get("verdict") else "") + "</li>"
                     for c in p["claims"])
    gaining = ", ".join(f'{e(g["label"][:70])} ({_fmt(g["views_6h"])} in 6h)' for g in p["gaining"])
    flagged = ", ".join(f'{e(f["label"][:60])} (control {f["score"]})' for f in p["flagged"])
    return (f'<div class="tr"><a class="t" href="../{e(p["slug"])}/">{e(p["title"])}</a> <span class="n">{p["posts_7d"]} posts · {_fmt(p["views_7d"])} views in 7d'
            + (f' · {p["false_7d"]} false claims' if p["false_7d"] else "") + f'</span><div>{lanes}</div>'
            + (f'<ul>{claims}</ul>' if claims else '<div class="dead">no claims this week</div>')
            + (f'<div class="hooks"><b>Electeds</b> ' + "; ".join(f'{e(x["name"])} ({e(x["office"][:28])}) in {e(x["lane"])}: <a href="{e(x["url"] or "#")}" style="color:var(--ink-2)">{e(x["text"][:90])}</a>' for x in p.get("electeds", [])) + '</div>' if p.get("electeds") else "")
            + (f'<div class="hooks"><b>Message-doc phrases</b> ' + "; ".join(f'“{e(x["phrase"])}” {x["posts"]} posts / {x["articles"]} articles' for x in p.get("phrases", [])) + '</div>' if p.get("phrases") else "")
            + (f'<div class="hooks"><b>Gaining now</b> {gaining}</div>' if gaining else "")
            + (f'<div class="hooks"><b>Flagged pushes</b> {flagged}</div>' if flagged else "")
            + f' <a href="../{e(p["slug"])}/claims.html" style="font-size:11px;color:#8cf">claims</a> · <a href="../{e(p["slug"])}/analysis.html" style="font-size:11px;color:#8cf">timing &amp; control</a></div>')


def render(out: dict[str, Any]) -> Path:
    e = html.escape
    blocks, former = [], []
    for c in out["clients"]:
        areas = []
        for a in c.get("areas", []):
            trackers = "".join(_tracker_html(out["trackers"].get(s, {"slug": s, "exists": False})) for s in a.get("trackers", []))
            hooks = ""
            if a.get("legislation"):
                hooks += f'<div class="hooks"><b>Legislation</b> {e(str(a["legislation"]))}</div>'
            if a.get("voices"):
                hooks += f'<div class="hooks"><b>Voices</b> {e(", ".join(str(v) for v in a["voices"]))}</div>'
            areas.append(f'<div class="area"><h3>{e(a["area"])}</h3><p class="why">{e(a.get("why") or "")}</p><div class="row"><div>{trackers}</div><div>{hooks}</div></div></div>')
        meta = " · ".join(x for x in (c.get("practice"), f'lead: {c["lead"]}' if c.get("lead") else "", f'notices: {c["notice"]}' if c.get("notice") else "") if x)
        confirm = f'<div class="confirm">To confirm: {e(str(c["confirm"]))}</div>' if c.get("confirm") else ""
        if c.get("status") == "former":
            former.append(e(c["name"]))
            continue
        if not c.get("areas") and not confirm:
            areas.append('<div class="dead">no policy area mapped yet</div>')
        blocks.append(f'<section class="client"><h2>{e(c["name"])} <span class="status {e(c.get("status", "client"))}">{e(c.get("status", "client"))}</span></h2>'
                      f'<div class="meta">{e(meta)}</div>{confirm}{"".join(areas)}</section>')
    if former:
        blocks.append(f'<section class="client"><h2>Former clients</h2><div class="meta">{", ".join(former)}</div></section>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clients — Texas Narrative Trackers</title><style>{CSS}</style></head><body>
<h1>Client desk: what is moving on the issues our clients care about</h1>
<p class="lead">Each client, the policy areas it lives on, the trackers watching them, what moved in the last seven days, and the legislative hooks and voices to watch.
Edited in <code>incidents/clients.yaml</code>; the race trackers end Nov 3, these run through the 2027 session.</p>
{"".join(blocks)}
<footer>Generated {e(out["generated_at"][:16].replace("T", " "))} UTC · {len(out["clients"])} clients and prospects · {sum(1 for p in out["trackers"].values() if p.get("live"))} of {len(out["trackers"])} trackers live.</footer>
</body></html>"""
    path = OUT_DIR / "clients" / "index.html"
    path.write_text(doc, encoding="utf-8")
    return path


def clients() -> Path:
    out = build()
    page = render(out)
    live = sum(1 for p in out["trackers"].values() if p.get("live"))
    print(f"wrote {page}  ({len(out['clients'])} clients, {live}/{len(out['trackers'])} trackers live)")
    return page
