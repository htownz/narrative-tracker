"""Produce a single self-contained HTML page: web/index.html with data.json inlined.

Avatars for the most-viewed posts are fetched and embedded as data URIs (about 2–4 KB each at the
48px size) so the page needs no outside image host — hosted viewers often block those.
"""
from __future__ import annotations

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .config import WEB_DIR, Incident

MARKER = "/*__DATA__*/"
AVATAR_TOP_N = 40


def _fetch_avatar(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 narrative-tracker"})
        if r.status_code != 200 or not r.content or len(r.content) > 60_000:
            return None
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(r.content).decode('ascii')}"
    except Exception:
        return None


def inline_avatars(data: dict[str, Any], top_n: int = AVATAR_TOP_N) -> int:
    posts = [p for p in data.get("posts", []) if p.get("avatar", "").startswith("http")]
    posts.sort(key=lambda p: -int(p.get("views") or 0))
    top = posts[:top_n]
    rest = posts[top_n:]
    urls = {p["avatar"] for p in top}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fetched = dict(zip(urls, ex.map(_fetch_avatar, urls)))
    n = 0
    for p in top:
        uri = fetched.get(p["avatar"])
        if uri:
            p["avatar"] = uri
            n += 1
        else:
            p["avatar"] = ""
    for p in rest:          # never drawn; drop the URL so the page carries no external references
        p["avatar"] = ""
    return n


def render(inc: Incident, avatars: bool = True) -> Path:
    data_path = inc.out_dir / "data.json"
    if not data_path.exists():
        raise SystemExit("no data.json yet — run export first")
    template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    if MARKER not in template:
        raise SystemExit("web/index.html is missing the /*__DATA__*/ marker")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    if avatars and not data.get("meta", {}).get("synthetic"):
        n = inline_avatars(data)
        print(f"inlined {n} avatars", file=sys.stderr)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace(MARKER, f"window.__DATA__ = {payload};")
    html = html.replace("<title>Narrative Tracker</title>", f"<title>{inc.title}</title>", 1)
    out = inc.out_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  ({out.stat().st_size // 1024} KB)")
    return out
