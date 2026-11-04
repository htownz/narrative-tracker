"""Election litigation tracker.

Pulls the sources in election/litigation.yaml, keeps the election-related items, attaches them to matters, and
writes out/election/litigation.json. Store: data/election/litigation.sqlite.

  python -m election litigation            # pull everything, export
  python -m election litigation --export   # export only

Sources: Democracy Docket's Texas case list (matters, parsed from each case page), the four Texas federal
districts' public ECF RSS feeds (every new docket entry; filtered to election matters), the Attorney General's
and Secretary of State's releases, Election Law Blog, the Texas Tribune and Votebeat. CourtListener needs an API
token (COURTLISTENER_TOKEN) and is not wired yet.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from . import DATA_DIR, OUT_DIR

CFG_PATH = Path(__file__).resolve().parent / "litigation.yaml"
DB_PATH = DATA_DIR / "litigation.sqlite"
UA = "Mozilla/5.0 (compatible; grey-havens-election-dashboard/0.1; +https://github.com/htownz/narrative-tracker)"
TIMEOUT = 40
NOISE_RE = re.compile(r"certified mail|summons (issued|returned)|notice of appearance|pro hac vice|auto docketing|digital audio|clerk'?s notice|scheduling order", re.I)
TEXAS_RE = re.compile(r"\btexas\b|\bharris county\b|\btarrant\b|\bbexar\b|\bdallas\b|\btravis\b|\bhouston\b|\bpaxton\b|\bhowden\b|\babbott\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS matters (
    id TEXT PRIMARY KEY, title TEXT, case_name TEXT, court TEXT, docket TEXT, category TEXT, status TEXT,
    filed TEXT, decided TEXT, summary TEXT, background TEXT, why TEXT, updates TEXT, documents TEXT,
    source TEXT, source_url TEXT, match TEXT, races TEXT, trackers TEXT, counties TEXT, last_update TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS items (
    url TEXT PRIMARY KEY, source TEXT, kind TEXT, court TEXT, docket TEXT, title TEXT, summary TEXT, published TEXT,
    fetched_at TEXT, matter_id TEXT);
CREATE TABLE IF NOT EXISTS pulls (at TEXT, source TEXT, ok INTEGER, items INTEGER, new INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS ix_items_pub ON items (published);
"""
EXTRA_COLS = {"matters": [("next_date", "TEXT"), ("next_text", "TEXT")]}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cfg() -> dict[str, Any]:
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for table, cols in EXTRA_COLS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    return c


def get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_date(s: str, url: str = "") -> str | None:
    """ISO date/datetime, or None when the string is not a date. SOS release URLs carry the date (…/2026/102726.shtml)."""
    s = (s or "").strip()
    for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.replace("Sept.", "Sep."), fmt).date().isoformat()
        except ValueError:
            pass
    if s:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    m = re.search(r"/(20\d\d)/(\d\d)(\d\d)\d\d\.shtml", url or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# ---------------------------------------------------------------------------------------------------------------
# feeds

def parse_rss(content: bytes) -> list[dict[str, Any]]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        if link:
            out.append({"url": link, "title": strip(it.findtext("title") or ""),
                        "summary": strip(it.findtext("description") or it.findtext("content:encoded", namespaces=ns) or "")[:700],
                        "published": it.findtext("pubDate") or it.findtext("dc:date", namespaces=ns) or ""})
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link = next((l.get("href") for l in e.findall("atom:link", ns) if l.get("rel") in (None, "alternate") and l.get("href")), "")
        if link:
            out.append({"url": link, "title": strip(e.findtext("atom:title", default="", namespaces=ns)),
                        "summary": strip(e.findtext("atom:summary", default="", namespaces=ns) or e.findtext("atom:content", default="", namespaces=ns))[:700],
                        "published": e.findtext("atom:updated", default="", namespaces=ns) or e.findtext("atom:published", default="", namespaces=ns)})
    return out


def pull_rss(src: dict[str, Any], rel: re.Pattern) -> list[dict[str, Any]]:
    items = parse_rss(get(src["url"]).content)
    out = []
    for it in items:
        text = f"{it['title']} {it['summary']}"
        if not rel.search(text):
            continue
        if src.get("require_texas") and not TEXAS_RE.search(text):
            continue
        out.append({**it, "kind": "rss", "court": None, "docket": None})
    return out


def pull_pacer(src: dict[str, Any], rel: re.Pattern) -> list[dict[str, Any]]:
    """Every new docket entry in the district. Keep entries whose case name or docket text is election-related
    and not routine service noise. Title looks like '1:26-cv-01234 Party v. Party'."""
    items = parse_rss(get(src["url"]).content)
    out = []
    for it in items:
        title, desc = it["title"], it["summary"]
        if NOISE_RE.search(desc) and not rel.search(title):
            continue
        if not (rel.search(title) or rel.search(desc)):
            continue
        if not (TEXAS_RE.search(title + " " + desc) or re.search(r"secretary of state|election|ballot|voter|voting", title, re.I)):
            continue
        m = re.match(r"^(\d:\d\d-[a-z]{2}-\d+(?:-\d+)?)\s+(.*)$", title)
        docket, case = (m.group(1), m.group(2)) if m else (None, title)
        out.append({"url": it["url"], "title": f"{case} — {desc[:160]}" if desc else case, "summary": desc[:700],
                    "published": it["published"], "kind": "pacer", "court": src.get("court"), "docket": docket})
    return out


def pull_ag(src: dict[str, Any], rel: re.Pattern) -> list[dict[str, Any]]:
    t = get(src["url"]).text
    out = []
    for m in re.finditer(r'<a href="(/news/releases/[^"]+)"[^>]*>(.*?)</a>', t, re.S):
        href, title = m.group(1), strip(m.group(2)).replace("\u00ad", "")
        if not title or not rel.search(title):
            continue
        # a date usually sits within a few hundred characters after the link
        tail = strip(t[m.end(): m.end() + 600])
        dm = re.search(r"([A-Z][a-z]+\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4})", tail)
        out.append({"url": "https://www.texasattorneygeneral.gov" + href, "title": title, "summary": "", "published": dm.group(1) if dm else "",
                    "kind": "ag", "court": None, "docket": None})
    # dedupe by url, keep first
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    return uniq


# ---------------------------------------------------------------------------------------------------------------
# Democracy Docket matters

DD_STATUSES = ("Filed", "Decided", "Appealed", "Victory", "Dismissed", "Settled", "Pending", "Withdrawn")
NAV_WORDS = {"Subscribe", "Support", "Menu", "Close", "Search", "Home", "Cases", "News", "Opinion", "Analysis", "Voting", "Litigation", "Legislation", "Redistricting"}


def dd_list(src: dict[str, Any]) -> list[dict[str, str]]:
    """[{slug, url, title}] across the list pages."""
    base = src["url"]
    out, seen = [], set()
    for page in range(1, int(src.get("pages") or 1) + 1):
        url = base if page == 1 else base.replace("/cases/?", f"/cases/page/{page}/?")
        try:
            t = get(url).text
        except Exception as e:  # noqa: BLE001
            print(f"[litigation] DD list page {page}: {type(e).__name__}: {e}", file=sys.stderr)
            break
        found = 0
        for m in re.finditer(r'<a[^>]+href="(https://www\.democracydocket\.com/cases/([a-z0-9-]+)/)"[^>]*>(.*?)</a>', t, re.S):
            href, slug, inner = m.group(1), m.group(2), strip(m.group(3))
            if slug in seen or slug in ("case-status",) or not inner:
                continue
            seen.add(slug); found += 1
            out.append({"slug": slug, "url": href, "title": inner[:200]})
        if not found:
            break
        time.sleep(0.5)
    return out


def dd_case(url: str) -> dict[str, Any]:
    """Parse one case page into matter fields."""
    raw = get(url).text
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    docs_by_url: dict[str, str] = {}
    for m in re.finditer(r'<a[^>]+href="([^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>', body, re.S | re.I):
        label = re.sub(r"\s*\(opens in new window\)\s*", "", strip(m.group(2)))
        if label and label.lower() not in ("more", "filed", "ruled", "issued", "granted", "denied"):
            if len(label) > len(docs_by_url.get(m.group(1), "")):
                docs_by_url[m.group(1)] = label[:160]
        elif m.group(1) not in docs_by_url:
            docs_by_url[m.group(1)] = ""
    docs = [{"label": label or re.sub(r".*/", "", url), "url": url} for url, label in docs_by_url.items()]
    txt = strip(re.sub(r"<[^>]+>", " | ", body))
    title = strip(re.search(r"<title>(.*?)</title>", raw, re.S).group(1)) if re.search(r"<title>", raw) else ""
    title = re.sub(r"^Court Cases - ", "", title).replace(" - Democracy Docket", "").strip()
    rec: dict[str, Any] = {"title": title, "source_url": url}
    # category | status | title | case name | Filed: date | Decided: date
    m = re.search(r"\|\s*([A-Z][A-Za-z\- ]{3,40})\s*\|(?:\s*\|)*\s*(" + "|".join(DD_STATUSES) + r")\s*\|(?:\s*\|)*\s*" + re.escape(title) + r"\s*\|(?:\s*\|)*\s*([^|]{3,160}?)\s*\|", txt)
    if m:
        rec["category"], rec["status"], rec["case_name"] = m.group(1).strip(), m.group(2), m.group(3).strip()
    else:
        sm = re.search(r"\|\s*(" + "|".join(DD_STATUSES) + r")\s*\|", txt)
        rec["status"] = sm.group(1) if sm else None
        cm = re.search(re.escape(title) + r"\s*\|(?:\s*\|)*\s*([^|]{3,160}?)\s*\|", txt)
        rec["case_name"] = cm.group(1).strip() if cm else None
    fm = re.search(r"Filed:\s*\|(?:\s*\|)*\s*([A-Z][a-z]+\.? \d{1,2}, \d{4})", txt)
    dm = re.search(r"Decided:\s*\|(?:\s*\|)*\s*([A-Z][a-z]+\.? \d{1,2}, \d{4})", txt)
    rec["filed"] = parse_date(fm.group(1)) if fm else None
    rec["decided"] = parse_date(dm.group(1)) if dm else None
    sm = re.search(r"Share on Bluesky\s*\|(?:\s*\|)*\s*([^|]{20,600}?)\s*\|", txt)
    rec["summary"] = sm.group(1).strip() if sm else None
    bm = re.search(r"\|\s*Background\s*\|(?:\s*\|)*\s*(.*?)\s*\|\s*(Why It Matters|Latest Updates|Case Documents)", txt, re.S)
    rec["background"] = strip(bm.group(1))[:2500] if bm else None
    wm = re.search(r"\|\s*Why It Matters\s*\|(?:\s*\|)*\s*(.*?)\s*\|\s*(Latest Updates|Case Documents)", txt, re.S)
    rec["why"] = strip(wm.group(1))[:800] if wm else None
    ups = []
    um = re.search(r"Latest Updates:?\s*\|(.*?)\|\s*Case Documents", txt, re.S)
    if um:
        for u in re.finditer(r"([A-Z][a-z]+\.? \d{1,2}, \d{4}):\s*\|?\s*(.*?)(?=\|\s*[A-Z][a-z]+\.? \d{1,2}, \d{4}:|$)", um.group(1), re.S):
            text = strip(u.group(2).replace("|", " "))
            if text:
                ups.append({"date": parse_date(u.group(1)), "text": text[:400]})
    rec["updates"] = ups
    rec["documents"] = docs[:40]
    today = datetime.now(timezone.utc).date().isoformat()
    past = [u["date"] for u in ups if u["date"] and u["date"] <= today] + [d for d in (rec["filed"], rec["decided"]) if d and d <= today]
    rec["last_update"] = max(past) if past else None
    future = sorted(u["date"] for u in ups if u["date"] and u["date"] > today)
    rec["next_date"] = future[0] if future else None
    rec["next_text"] = next((u["text"] for u in ups if u["date"] == rec["next_date"]), None) if future else None
    # court: DD rarely states it in text; infer from documents/background
    cm = re.search(r"(U\.S\. District Court for the [A-Za-z ]+ District of Texas|[A-Z][a-z]+ County District Court|Texas Supreme Court|Fifth Circuit|U\.S\. Supreme Court|Court of Appeals)", (rec.get("background") or "") + " " + txt[:4000])
    rec["court"] = cm.group(1) if cm else ("Federal court" if re.search(r"federal (lawsuit|court|suit)", rec.get("background") or "", re.I) else None)
    return rec


GENERIC = {"texas", "county", "challenge", "election", "elections", "voter", "voters", "voting", "state", "request", "extension", "polling",
           "hours", "ballot", "ballots", "mail", "law", "court", "cases", "case", "united", "states", "district", "federal", "party", "republican",
           "democratic", "abbott", "paxton", "nelson", "howden", "secretary", "office", "attorney", "general", "harris", "tarrant", "bexar", "dallas",
           "travis", "houston", "austin", "city", "board", "commissioners", "registration", "closure", "place", "suppression", "information",
           "disclosure", "accessibility", "maintenance", "roll", "rolls", "system", "purge", "delay", "limitations", "order", "executive",
           "citizenship", "status", "access", "league", "women", "conference", "caucus", "legislative", "american", "mexican", "latin",
           "citizens", "justice", "fund", "civic", "common", "cause", "certification", "redistricting", "congressional", "primary", "runoff"}


COMMON = {"first", "national", "america", "american", "americans", "policy", "initiative", "institute", "federation", "people", "peoples",
          "project", "group", "center", "centre", "foundation", "coalition", "alliance", "association", "council", "committee", "department",
          "fire", "housing", "authority", "rights", "civil", "liberties", "union", "united", "voters", "vote", "action", "advancement",
          "progress", "freedom", "liberty", "public", "interest", "defense", "legal", "education", "educational", "network", "organizing",
          "workers", "families", "community", "communities", "immigrant", "immigrants", "latino", "latinos", "hispanic", "black", "asian",
          "college", "university", "students", "student", "young", "youth", "senior", "seniors", "disability", "disabilities", "blind",
          "texans", "county", "counties", "church", "churches", "campaign", "campaigns", "inc", "llc", "corp", "company", "president",
          "governor", "senate", "house", "congress", "congressional", "judge", "clerk", "administrator", "commissioner", "commission",
          "mayor", "sheriff", "district", "city", "town", "village", "school", "board", "trustees", "independent", "biden", "trump", "harris"}
PARTY_SUFFIX = re.compile(r"\b(et al\.?|inc\.?|llc|l\.l\.c\.|corp\.?|co\.|ltd\.?|pllc|pc)\b\.?$", re.I)


def _last_token(side: str) -> str:
    s = PARTY_SUFFIX.sub("", strip(side).split(",")[0]).strip(" .")
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z'\-]+", s) if t.lower() not in GENERIC | COMMON]
    return toks[-1] if toks else ""


def matter_match(case_name: str | None, title: str, extra: list[str] | None = None) -> str:
    """JSON: {pair: regex for 'A v. B', words: distinctive party words, rare: the words that count on their own,
    keywords: hand-added phrases}. Coverage = pair hit, or a keyword hit, or two word hits of which one is rare."""
    words = set()
    for src in (case_name or "", title or ""):
        for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", src):
            if w.lower() in GENERIC or w.lower() in COMMON:
                continue
            words.add(w)
    rare = sorted(w for w in words if w[:1].isupper() and (case_name or "").find(w) >= 0)
    pair = None
    if case_name and " v. " in case_name:
        a, b = case_name.split(" v. ", 1)
        a_last, b_last = _last_token(a), _last_token(b)
        if a_last and b_last:
            pair = re.escape(a_last) + r"\b.{0,60}\bv\.?\s.{0,60}\b" + re.escape(b_last)
    return json.dumps({"pair": pair, "words": sorted(words), "rare": rare, "keywords": extra or []})


def matter_hit(match: str | None, text: str) -> bool:
    if not match:
        return False
    try:
        spec = json.loads(match)
    except (TypeError, ValueError):                       # a hand-written regex (the `matters:` list in the YAML)
        return bool(re.search(match, text, re.I))
    if not isinstance(spec, dict):
        return False
    if spec.get("pair") and re.search(spec["pair"], text, re.I):
        return True
    for kw in spec.get("keywords") or []:
        if re.search(r"\b" + re.escape(kw) + r"\b", text, re.I):
            return True
    hits = {w for w in spec.get("words") or [] if re.search(r"\b" + re.escape(w) + r"\b", text, re.I)}
    rare = set(spec.get("rare") or [])
    return len(hits) >= 2 and bool(hits & rare)


# ---------------------------------------------------------------------------------------------------------------

def races_for(c: dict[str, Any], counties: list[str], category: str | None, text: str) -> list[str]:
    cr = c.get("county_races") or {}
    out: list[str] = []
    for county in counties:
        out += cr.get(county, [])
    if re.search(r"certif|statewide|secretary of state|hand count|redistrict|congressional", text, re.I):
        out += cr.get("statewide", [])
        if re.search(r"congressional|redistrict|federal election", text, re.I):
            out += [f"cd-{n}" for n in (9, 15, 23, 28, 34, 35)]
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq


def pull(export_only: bool = False) -> dict[str, Any]:
    c = cfg()
    rel = re.compile(c.get("relevance") or "election", re.I | re.X)
    since = str(c.get("since") or "")
    conn = db()
    if since and not export_only:
        conn.execute("DELETE FROM items WHERE published IS NOT NULL AND substr(published,1,10) < ?", (since,))
        conn.execute("DELETE FROM items WHERE published IS NULL AND kind != 'pacer'")
        conn.commit()
    at = now()
    report: dict[str, Any] = {"at": at, "sources": {}}
    if not export_only:
        # 1. hand-maintained matters
        for m in c.get("matters") or []:
            conn.execute("""INSERT INTO matters (id, title, case_name, court, category, status, summary, source, source_url, match, races, trackers, counties, fetched_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET title=excluded.title, case_name=excluded.case_name, court=excluded.court, category=excluded.category,
                              status=excluded.status, summary=excluded.summary, source=excluded.source, source_url=excluded.source_url, match=excluded.match,
                              races=excluded.races, trackers=excluded.trackers, counties=excluded.counties""",
                         (m["id"], m.get("title"), m.get("case_name"), m.get("court"), m.get("category"), m.get("status"), m.get("summary"), m.get("source"),
                          m.get("source_url"), m.get("match"), json.dumps(m.get("races") or []), json.dumps(m.get("trackers") or []), json.dumps(m.get("counties") or []), at))
        conn.commit()
        # 2. sources
        for src in c.get("sources") or []:
            name, kind = src["name"], src["kind"]
            try:
                if kind == "dd_cases":
                    cases = dd_list(src)
                    stale_before = time.time() - 3600 * float(src.get("refetch_hours") or 6)
                    fetched = new = 0
                    for case in cases:
                        mid = "dd-" + case["slug"]
                        row = conn.execute("SELECT fetched_at FROM matters WHERE id=?", (mid,)).fetchone()
                        if row and row["fetched_at"] and datetime.fromisoformat(row["fetched_at"]).timestamp() > stale_before:
                            continue
                        try:
                            rec = dd_case(case["url"])
                        except Exception as e:  # noqa: BLE001
                            print(f"[litigation] DD case {case['slug']}: {type(e).__name__}: {e}", file=sys.stderr)
                            continue
                        fetched += 1
                        if not row:
                            new += 1
                        ov = (c.get("dd_overrides") or {}).get(case["slug"]) or {}
                        for k in ("court", "docket", "title", "category", "filed", "status"):
                            if ov.get(k):
                                rec[k] = str(ov[k])
                        if rec.get("category") in NAV_WORDS:
                            rec["category"] = None
                        text = " ".join(str(rec.get(k) or "") for k in ("title", "case_name", "summary", "background"))
                        counties = sorted({cn for cn in (c.get("county_races") or {}) if cn != "statewide" and re.search(r"\b" + cn + r"\b", text, re.I)})
                        conn.execute("""INSERT INTO matters (id, title, case_name, court, docket, category, status, filed, decided, summary, background, why, updates, documents,
                                                             source, source_url, match, races, trackers, counties, last_update, fetched_at, next_date, next_text)
                                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                        ON CONFLICT(id) DO UPDATE SET title=excluded.title, case_name=excluded.case_name, court=excluded.court, docket=excluded.docket,
                                          category=excluded.category, status=excluded.status, filed=excluded.filed, decided=excluded.decided, summary=excluded.summary,
                                          background=excluded.background, why=excluded.why, updates=excluded.updates, documents=excluded.documents, match=excluded.match,
                                          races=excluded.races, counties=excluded.counties, last_update=excluded.last_update, fetched_at=excluded.fetched_at,
                                          next_date=excluded.next_date, next_text=excluded.next_text""",
                                     (mid, rec.get("title") or case["title"], rec.get("case_name"), rec.get("court"), rec.get("docket"), rec.get("category"), rec.get("status"),
                                      rec.get("filed"), rec.get("decided"), rec.get("summary"), rec.get("background"), rec.get("why"), json.dumps(rec.get("updates") or []),
                                      json.dumps(rec.get("documents") or []), "Democracy Docket", case["url"],
                                      matter_match(rec.get("case_name"), rec.get("title") or case["title"], ov.get("keywords")),
                                      json.dumps(list(dict.fromkeys((ov.get("races") or []) + races_for(c, counties, rec.get("category"), text)))), json.dumps(["issue-election-integrity-live"]),
                                      json.dumps(counties), rec.get("last_update"), at, rec.get("next_date"), rec.get("next_text")))
                        conn.commit()
                        time.sleep(0.5)
                    conn.execute("INSERT INTO pulls VALUES (?,?,?,?,?,?)", (at, name, 1, len(cases), new, f"{fetched} case pages fetched"))
                    report["sources"][name] = {"ok": True, "cases": len(cases), "fetched": fetched, "new": new}
                    continue
                items = {"rss": pull_rss, "pacer_rss": pull_pacer, "ag_releases": pull_ag}[kind](src, rel)
                new = 0
                for it in items:
                    pub = parse_date(it.get("published") or "", it["url"])
                    if since and pub and pub[:10] < since:
                        continue
                    if since and not pub and kind != "pacer_rss":
                        continue                          # undated, from a feed that reaches back years: skip
                    cur = conn.execute("INSERT OR IGNORE INTO items (url, source, kind, court, docket, title, summary, published, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                       (it["url"], name, it["kind"], it.get("court"), it.get("docket"), it["title"], it.get("summary"), pub, at))
                    new += cur.rowcount
                conn.commit()
                conn.execute("INSERT INTO pulls VALUES (?,?,?,?,?,?)", (at, name, 1, len(items), new, ""))
                report["sources"][name] = {"ok": True, "items": len(items), "new": new}
            except Exception as e:  # noqa: BLE001
                conn.execute("INSERT INTO pulls VALUES (?,?,?,?,?,?)", (at, name, 0, 0, 0, f"{type(e).__name__}: {str(e)[:200]}"))
                report["sources"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            conn.commit()
        # 3. attach items to matters
        matters = conn.execute("SELECT id, match FROM matters").fetchall()
        conn.execute("UPDATE items SET matter_id=NULL")   # re-attach every run: matter patterns improve as cases are refetched
        for it in conn.execute("SELECT url, title, summary FROM items").fetchall():
            text = f"{it['title']} {it['summary'] or ''}"
            hit = next((m["id"] for m in matters if matter_hit(m["match"], text)), None)
            if hit:
                conn.execute("UPDATE items SET matter_id=? WHERE url=?", (hit, it["url"]))
        conn.commit()
    out = export(conn)
    conn.close()
    report["matters"] = len(out["matters"]); report["items"] = out["item_count"]
    return report


def export(conn: sqlite3.Connection) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    matters = []
    for m in conn.execute("SELECT * FROM matters").fetchall():
        d = dict(m)
        for k in ("updates", "documents", "races", "trackers", "counties"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except Exception:  # noqa: BLE001
                d[k] = []
        d.pop("match", None)
        d["coverage"] = [dict(r) for r in conn.execute("SELECT url, source, kind, court, docket, title, published FROM items WHERE matter_id=? ORDER BY published DESC LIMIT 15", (m["id"],)).fetchall()]
        last_cov = max([x["published"] or "" for x in d["coverage"]] or [""])
        d["activity"] = max(d.get("last_update") or "", last_cov[:10] if last_cov else "")
        matters.append(d)
    matters.sort(key=lambda d: (d.get("activity") or "", d.get("filed") or ""), reverse=True)
    recent = [dict(r) for r in conn.execute("SELECT url, source, kind, court, docket, title, summary, published, matter_id FROM items ORDER BY published DESC, fetched_at DESC LIMIT 150").fetchall()]
    pulls = [dict(r) for r in conn.execute("SELECT * FROM pulls WHERE at = (SELECT MAX(at) FROM pulls)").fetchall()]
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    out = {"generated_at": now(), "matters": matters, "recent_items": recent, "item_count": count, "pulls": pulls,
           "note": "Matters from Democracy Docket carry its descriptions and its pro-/anti-voting framing; parties, court, dates and documents are the facts to use."}
    (OUT_DIR / "litigation.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return out


if __name__ == "__main__":
    rep = pull(export_only="--export" in sys.argv)
    print(json.dumps(rep, indent=1))
