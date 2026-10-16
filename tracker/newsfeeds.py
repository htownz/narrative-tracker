"""A store of press articles from RSS (incidents/news_feeds.yaml) and the relevance test that decides whether
an article is about a claim.

Why a store: Google News search reaches back seven days, so a claim's 48h coverage window could only be
measured for claims first seen in the last five. Articles kept here accumulate from the day the feed pull
starts, so the window is measurable for every claim after that — and the outlet set is the Texas press,
not whatever Google ranks.

Why a relevance test: a query built from two names finds every article about the race. "Press first" on a
claim means an article about *that claim* ran before the first post on X, so an article has to share the
claim's distinctive words, not just its names.

pull_feeds()                 -> fetch every feed, insert new articles into data/news.sqlite
relevant(article, claim, entities) -> bool
search(store, claim, entities, since, until) -> matching articles as {seendate, domain, title, url}
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from .config import DATA_DIR, ROOT

FEEDS_PATH = ROOT / "incidents" / "news_feeds.yaml"
DB_PATH = DATA_DIR / "news.sqlite"
TIMEOUT = 20
STOP = set("""a an the and or but of to in on at for with by from as is are was were be been being this that these those it its he
she they them his her their we you i our your not no do does did have has had will would can could should about into over
than then so if just more most very who whom what which when where why how all any some such own same says said say up out off
down vs after before during while because against between through under again further once here there also texas state
new says said told asked according report reports reported news week year years today yesterday tomorrow monday tuesday
wednesday thursday friday saturday sunday among would could should still already another every each much many first last
race campaign election senate governor republican democrat democratic candidate candidates voters voter poll polls""".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    domain TEXT,
    feed TEXT,
    title TEXT,
    summary TEXT,
    published TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE TABLE IF NOT EXISTS feed_log (
    feed TEXT,
    at TEXT,
    status TEXT,
    items INTEGER,
    new INTEGER
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.executescript(SCHEMA)
    return conn


def _parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_feed(content: bytes) -> list[dict[str, Any]]:
    """RSS 2.0 or Atom -> [{url, title, summary, published}]."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out = []
    ns = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}
    for it in root.iter("item"):                               # RSS
        link = (it.findtext("link") or "").strip()
        if not link:
            continue
        src = it.find("source")
        out.append({"url": link, "title": _strip_html(it.findtext("title") or ""),
                    "summary": _strip_html(it.findtext("description") or it.findtext("content:encoded", namespaces=ns) or "")[:600],
                    "published": it.findtext("pubDate") or it.findtext("dc:date", namespaces=ns) or "",
                    "source_url": (src.get("url") or "") if src is not None else ""})
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):  # Atom
        link = ""
        for l in e.findall("atom:link", ns):
            if l.get("rel") in (None, "alternate") and l.get("href"):
                link = l.get("href"); break
        if not link:
            continue
        out.append({"url": link, "title": _strip_html(e.findtext("atom:title", default="", namespaces=ns)),
                    "summary": _strip_html(e.findtext("atom:summary", default="", namespaces=ns) or e.findtext("atom:content", default="", namespaces=ns))[:600],
                    "published": e.findtext("atom:published", default="", namespaces=ns) or e.findtext("atom:updated", default="", namespaces=ns)})
    return out


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc.lower().removeprefix("www.")


def discover_feed(html_text: str, base_url: str) -> str | None:
    """The RSS/Atom autodiscovery link in an HTML page, if any."""
    from urllib.parse import urljoin
    m = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', html_text, re.I)
    if not m:
        return None
    href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
    return urljoin(base_url, href.group(1)) if href else None


def gnews_url(domain: str) -> str:
    """Google News search RSS limited to one outlet: the fallback when an outlet blocks fetches or has no feed."""
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(f'site:{domain} when:3d')}&hl=en-US&gl=US&ceid=US:en"


def pull_feeds(path: Path = FEEDS_PATH, db: Path = DB_PATH, session: requests.Session | None = None) -> dict[str, int]:
    """Fetch every feed once; insert articles not seen before. Returns {feed name: new articles}."""
    if not path.exists():
        return {}
    feeds = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("feeds") or []
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; narrative-tracker/1.0; +https://github.com/htownz/narrative-tracker)")
    conn = connect(db)
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    result: dict[str, int] = {}
    failed = []
    def fetch(url: str) -> tuple[str, list[dict[str, Any]], bytes]:
        try:
            r = sess.get(url, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            return type(e).__name__, [], b""
        return str(r.status_code), (parse_feed(r.content) if r.status_code == 200 else []), (r.content if r.status_code == 200 else b"")

    for f in feeds:
        name, url = f.get("name") or f.get("url"), f.get("url")
        if not url:
            continue
        status, items, body = fetch(url)
        if status == "200" and not items and body:                 # an HTML page: follow its RSS autodiscovery link once
            alt = discover_feed(body.decode("utf-8", "ignore")[:200_000], url)
            if alt and alt != url:
                status, items, _ = fetch(alt)
                status = f"{status} via autodiscovery"
        if not items:                                               # blocked or no feed: that outlet's stories via Google News
            dom = f.get("domain") or domain_of(url)
            g_status, g_items, _ = fetch(gnews_url(dom))
            if g_items:
                status, items = f"gnews ({status})", g_items
        new = 0
        for it in items:
            d = _parse_date(it["published"])
            dom = domain_of(it.get("source_url") or "") if "news.google.com" in it["url"] else domain_of(it["url"])
            try:
                cur = conn.execute("INSERT OR IGNORE INTO articles (url, domain, feed, title, summary, published, fetched_at) VALUES (?,?,?,?,?,?,?)",
                                   (it["url"], dom or domain_of(it["url"]), name, it["title"][:300], it["summary"], d.isoformat() if d else now, now))
                new += 1 if cur.rowcount > 0 else 0
            except sqlite3.Error:
                continue
        conn.execute("INSERT INTO feed_log (feed, at, status, items, new) VALUES (?,?,?,?,?)", (name, now, status, len(items), new))
        result[name] = new
        if not items:
            failed.append(f"{name} ({status})")
        time.sleep(0.2)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    print(f"news feeds: {len(feeds) - len(failed)}/{len(feeds)} answered, {sum(result.values())} new articles, {total:,} stored"
          + (f"; no items from: {', '.join(failed)}" if failed else ""))
    return result


# ---- relevance ------------------------------------------------------------------------------------
def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", text or "") if w.lower() not in STOP and len(w) >= 4}


def _entity_tokens(entities: list[str]) -> set[str]:
    toks: set[str] = set()
    for e in entities or []:
        for w in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", str(e)):
            if w.lower() not in STOP and len(w) >= 4:
                toks.add(w.lower())
    return toks


def relevant(article: dict[str, Any], claim_text: str, entities: list[str], subjects: set[str] | None = None) -> bool:
    """An article is about the claim, not just about the race, when its title (plus summary):
      - names a specific entity of the claim (one that is not the tracker's own subject — the Lindner Center,
        a pollster, a bill) together with another entity or another distinctive word of the claim; or
      - names any entity of the claim and shares one of its other distinctive words; or
      - shares three of the claim's distinctive words outright."""
    text = f"{article.get('title') or ''} {article.get('summary') or ''}"
    aw = _words(text)
    if not aw:
        return False
    subj = {w for s_ in (subjects or set()) for w in _words(s_)} | {s_.lower() for s_ in (subjects or set())}
    ent_tokens: dict[str, set[str]] = {str(e): _entity_tokens([e]) for e in (entities or []) if str(e).strip()}
    matched = [e for e, toks in ent_tokens.items() if toks and aw & toks]
    specific = [e for e in matched if not (ent_tokens[e] & subj)]
    all_ent = set().union(*ent_tokens.values()) if ent_tokens else set()
    other_hit = len(aw & (_words(claim_text) - all_ent))
    if specific and (len(matched) >= 2 or other_hit >= 1):
        return True
    return (bool(matched) and other_hit >= 1) or other_hit >= 3


def search(claim_text: str, entities: list[str], since: datetime, until: datetime | None = None, db: Path = DB_PATH,
           subjects: set[str] | None = None) -> list[dict[str, Any]]:
    """Stored articles about the claim published between since and until, in news.summarise()'s shape."""
    if not db.exists():
        return []
    conn = connect(db)
    until = until or datetime.now(timezone.utc)
    ent = _entity_tokens(entities)
    cw = _words(claim_text)
    probe = sorted(ent | cw, key=len, reverse=True)[:6]            # cheap SQL prefilter, then the real test in Python
    if not probe:
        conn.close()
        return []
    where = " OR ".join("(lower(title) LIKE ? OR lower(summary) LIKE ?)" for _ in probe)
    args = [x for w in probe for x in (f"%{w}%", f"%{w}%")]
    rows = conn.execute(f"SELECT url, domain, title, summary, published FROM articles WHERE published >= ? AND published <= ? AND ({where})",
                        [since.isoformat(), until.isoformat()] + args).fetchall()
    conn.close()
    out = []
    for r in rows:
        a = dict(r)
        if relevant(a, claim_text, entities, subjects):
            out.append({"seendate": a["published"], "domain": a["domain"], "title": a["title"], "url": a["url"]})
    return out


def status(db: Path = DB_PATH) -> dict[str, Any]:
    if not db.exists():
        return {"articles": 0, "feeds_ok": 0, "since": None}
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    since = conn.execute("SELECT MIN(fetched_at) FROM articles").fetchone()[0]     # coverage starts when collection started, not at the oldest item a feed happened to carry
    last = conn.execute("SELECT MAX(at) FROM feed_log").fetchone()[0]
    ok = conn.execute("SELECT COUNT(DISTINCT feed) FROM feed_log WHERE at = ? AND items > 0", (last,)).fetchone()[0] if last else 0
    conn.close()
    return {"articles": n, "feeds_ok": ok, "since": since, "last": last}


if __name__ == "__main__":
    pull_feeds()
    print(status())
