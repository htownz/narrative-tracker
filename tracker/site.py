"""Assemble the published site: copy every tracker's pages into site/<slug>/, put one navigation bar on every
page (tracker switcher that keeps the current tab, tabs for the tracker's pages, home and amplifier-map links),
then build the hub.

    python -m tracker.site site        # after `python -m tracker.status site` and `python run.py accounts --all`
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import OUT_DIR, ROOT

PAGES = [("index.html", "Chart"), ("claims.html", "Claims"), ("accounts.html", "Who moved it"),
         ("analysis.html", "Timing & control"), ("review.html", "Review lanes")]

NAV_CSS = """<style id="nt-nav-css">
  #nt-nav { position:sticky; top:0; z-index:50; display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:-16px -16px 14px; padding:8px 16px;
            background:rgba(11,11,12,.94); backdrop-filter:blur(6px); border-bottom:1px solid #2a2a30; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; font-size:12.5px; }
  #nt-nav a { color:#b9b9b3; text-decoration:none; padding:4px 8px; border-radius:6px; white-space:nowrap; }
  #nt-nav a:hover { color:#f2f2f0; background:#1d1d21; } #nt-nav a.on { color:#f2f2f0; background:#1d1d21; box-shadow:inset 0 -2px 0 #e08a2e; }
  #nt-nav a.home { color:#f2f2f0; font-weight:600; padding-left:0; } #nt-nav .sep { color:#3a3a42; }
  #nt-nav select { background:#141416; color:#f2f2f0; border:1px solid #2a2a30; border-radius:6px; padding:4px 8px; font-size:12.5px; max-width:260px; }
  #nt-nav .tabs { display:flex; gap:2px; flex-wrap:wrap; } #nt-nav .right { margin-left:auto; display:flex; gap:2px; }
  @media (max-width: 640px) { #nt-nav { margin:-16px -16px 10px; } #nt-nav .right { margin-left:0; } }
</style>"""

NAV_JS = """<script>if (/[?&]video=1/.test(location.search)) { var n = document.getElementById('nt-nav'); if (n) n.style.display = 'none'; }</script>"""


def trackers() -> list[dict[str, Any]]:
    """Every rendered, non-synthetic tracker with its title, group and which pages exist."""
    out = []
    for d in sorted(OUT_DIR.iterdir()):
        if not d.is_dir() or d.name in ("accounts", "insights", "clients") or not (d / "index.html").exists() or not (d / "data.json").exists():
            continue
        try:
            meta = json.loads((d / "data.json").read_text(encoding="utf-8")).get("meta", {})
        except ValueError:
            continue
        if meta.get("synthetic"):
            continue
        yaml_path = ROOT / "incidents" / f"{d.name}.yaml"
        live = yaml_path.exists() and "end: today" in yaml_path.read_text(encoding="utf-8")
        group = "Issues" if d.name.startswith("issue-") else "Races and people" if live else "Archive"
        out.append({"slug": d.name, "title": meta.get("title", d.name), "live": live, "group": group,
                    "pages": [p for p, _ in PAGES if (d / p).exists()]})
    order = {"Races and people": 0, "Issues": 1, "Archive": 2}
    out.sort(key=lambda t: (order[t["group"]], t["title"].lower()))
    return out


def nav_html(all_trackers: list[dict[str, Any]], slug: str | None, page: str | None, depth: int = 1) -> str:
    """depth: how many directories deep the page sits under the site root (hub = 0, tracker pages = 1)."""
    root = "../" * depth
    by_group: dict[str, list[dict[str, Any]]] = {}
    for t in all_trackers:
        by_group.setdefault(t["group"], []).append(t)
    options = ['<option value="">Switch tracker…</option>']
    for g, items in by_group.items():
        options.append(f'<optgroup label="{html.escape(g)}">')
        for t in items:
            target = page if page and page in t["pages"] else "index.html"
            href = f"{root}{t['slug']}/{'' if target == 'index.html' else target}"
            sel = ' selected' if t["slug"] == slug else ""
            options.append(f'<option value="{html.escape(href)}"{sel}>{html.escape(t["title"])}{" · live" if t["live"] else ""}</option>')
        options.append("</optgroup>")
    tabs = ""
    cur = next((t for t in all_trackers if t["slug"] == slug), None)
    if cur:
        tabs = '<span class="sep">|</span><span class="tabs">' + "".join(
            f'<a href="{root}{slug}/{"" if p == "index.html" else p}" class="{"on" if p == page else ""}">{label}</a>'
            for p, label in PAGES if p in cur["pages"]) + "</span>"
    right = (f'<span class="right"><a href="{root}brief/" class="{"on" if slug == "brief" else ""}">Weekly brief</a>'
             f'<a href="{root}insights/" class="{"on" if slug == "insights" else ""}">Insights</a>'
             f'<a href="{root}clients/" class="{"on" if slug == "clients" else ""}">Clients</a>'
             f'<a href="{root}accounts/" class="{"on" if slug == "accounts" else ""}">Amplifier map</a></span>')
    return (f'{NAV_CSS}<nav id="nt-nav"><a class="home" href="{root}">Texas Narrative Trackers</a>'
            f'<select onchange="if(this.value)location.href=this.value">{"".join(options)}</select>{tabs}{right}</nav>{NAV_JS}')


def inject(page_html: str, nav: str) -> str:
    m = re.search(r"<body[^>]*>", page_html)
    if not m:
        return nav + page_html
    return page_html[:m.end()] + "\n" + nav + "\n" + page_html[m.end():]


def assemble(site_dir: Path) -> int:
    site_dir.mkdir(parents=True, exist_ok=True)
    all_t = trackers()
    n = 0
    for t in all_t:
        dest = site_dir / t["slug"]
        dest.mkdir(parents=True, exist_ok=True)
        for p in t["pages"]:
            src = OUT_DIR / t["slug"] / p
            (dest / p).write_text(inject(src.read_text(encoding="utf-8"), nav_html(all_t, t["slug"], p)), encoding="utf-8")
            n += 1
        for extra in ("data.json", "claims.json", "analysis.json", "control.json", "features.json"):
            if (OUT_DIR / t["slug"] / extra).exists():
                shutil.copy(OUT_DIR / t["slug"] / extra, dest / extra)
    for name in ("accounts", "insights", "clients"):  # cross-tracker pages: the amplifier map, the insights page, the client desk
        page = OUT_DIR / name / "index.html"
        if page.exists():
            (site_dir / name).mkdir(parents=True, exist_ok=True)
            (site_dir / name / "index.html").write_text(inject(page.read_text(encoding="utf-8"), nav_html(all_t, name, None)), encoding="utf-8")
            n += 1
    if (OUT_DIR / "insights.json").exists():
        shutil.copy(OUT_DIR / "insights.json", site_dir / "insights" / "insights.json") if (site_dir / "insights").exists() else None
    briefs = sorted((OUT_DIR / "brief").glob("*.html")) if (OUT_DIR / "brief").exists() else []
    if briefs:
        (site_dir / "brief").mkdir(parents=True, exist_ok=True)
        for b in briefs:
            (site_dir / "brief" / b.name).write_text(inject(b.read_text(encoding="utf-8"), nav_html(all_t, "brief", None)), encoding="utf-8")
            n += 1
        if (OUT_DIR / "brief" / "latest.json").exists():
            shutil.copy(OUT_DIR / "brief" / "latest.json", site_dir / "brief" / "latest.json")
    from .hub import build
    hub = build(site_dir)
    hub.write_text(inject(hub.read_text(encoding="utf-8"), nav_html(all_t, None, None, depth=0)), encoding="utf-8")
    print(f"site: {n} tracker pages + hub assembled in {site_dir} ({len(all_t)} trackers)")
    return n


if __name__ == "__main__":
    assemble(Path(sys.argv[1] if len(sys.argv) > 1 else "site"))
