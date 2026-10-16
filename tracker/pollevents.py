"""Poll releases as calendar events, found automatically in the claims.

A poll claim is a checkable fact that names a pollster and carries numbers ("A YouGov/Univision poll has
Talarico leading Paxton 48% to 43%"). The claims extractor already pulls these out of every tracker; this
module turns them into events: one per pollster per release (claims about the same pollster within MERGE_H of
each other are one release), timed at the first post on X that carried it (a lower bound, like the hand-entered
ones), applied to every tracker where the poll was discussed at POLL_MIN_VIEWS+ views. load_events() merges
them with incidents/events.yaml and drops an automatic one when a hand-entered poll event already covers it.

Why: polls are the event type that moves lanes most, and sixteen hand-entered ones is a thin base. This keeps
the calendar current without anyone editing a YAML file on poll day.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from .config import OUT_DIR

POLL_MIN_VIEWS = 50_000
MERGE_H = 36
POLL_RE = re.compile(r"\b(?:poll|polls|polling|survey)\b", re.I)
NUMBERS_RE = re.compile(r"\d+(?:\.\d+)?\s?%|\b\d+\s?(?:points?|pts)\b|\b\d{2}\s?[-–]\s?\d{2}\b")
# "An Overton Insights poll", "the same Fox News poll", "A UT/YouGov poll", "Leaked GOP polling" -> the pollster
POLLSTER_RE = re.compile(r"(?:\b(?:a|an|the|the same|new|latest|leaked)\s+)?([A-Z][\w&.'’-]*(?:[ /][A-Z][\w&.'’-]*){0,3})\s+(?:poll|polling|survey)\b")
GENERIC = {"a", "an", "the", "new", "same", "latest", "leaked", "democratic", "republican", "gop", "internal", "texas", "national", "recent", "in",
           "another", "this", "that", "one", "every", "their", "his", "her", "its", "our", "statewide", "early", "senate", "governor", "house", "sponsored",
           "post", "labor", "day", "week", "weekend", "month", "june", "july", "august", "september", "october", "november", "december", "january",
           "february", "march", "april", "may", "first", "second", "last", "tracking", "daily", "weekly", "independent", "county", "state", "federal"}
FILLER = {"poll", "polling", "polls", "survey", "group", "research", "insights", "strategies", "intelligence", "university", "of", "project", "school",
          "public", "news", "report", "political", "center", "institute", "analytics", "associates", "partners", "consulting", "and", "llc", "inc"}
AVERAGE_RE = re.compile(r"\b(average|aggregate|aggregat\w+|model|forecast)\b", re.I)


def pollster(text: str) -> str | None:
    """The pollster named before the word poll, or None when the text only says 'a poll' or names no one."""
    if AVERAGE_RE.search(text):
        return None
    for m in POLLSTER_RE.finditer(text):
        name = _clean(m.group(1).strip(" .,'’"))
        if name and any(w.lower() not in FILLER for w in re.split(r"[ /-]", name)):
            return name
    return None


def _generic(word: str) -> bool:
    return all(piece.lower() in GENERIC for piece in word.split("-") if piece)


def _clean(name: str) -> str:
    """Drop generic words and trailing poll/survey words, keeping the pollster's own spelling and slashes."""
    segs = []
    for seg in name.split("/"):
        words = [w for w in seg.split() if w and not _generic(w) and w.lower() not in ("poll", "polls", "polling", "survey")]
        if words:
            segs.append(" ".join(words))
    return "/".join(segs)


def tokens(name: str) -> set[str]:
    """What identifies a pollster across spellings: YouGov/Univision and Univision/YouGov share both tokens."""
    out = {t for t in re.split(r"[ /\-]", name.lower()) if t and t not in FILLER and t not in GENERIC and len(t) >= 2}
    for seg in name.lower().split("/"):                      # "Insider Advantage" and "InsiderAdvantage" are one pollster
        squashed = re.sub(r"[^a-z0-9]", "", seg)
        if len(squashed) >= 6:
            out.add(squashed)
    return out


def subject_tokens(queries: list[str]) -> set[str]:
    """Names a tracker is about, from its search queries: quoted phrases and capitalised words."""
    out: set[str] = set()
    for q in queries:
        for ph in re.findall(r'"([^"]+)"', q):
            ph = re.sub(r"\b(texas|gov\.?|lt\.?)\b", "", ph, flags=re.I).strip(" .")
            if len(ph) >= 4:
                out.add(ph.lower())
        for w in re.findall(r"\b[A-Z][A-Za-z]{3,}\b", re.sub(r'"[^"]+"', "", q)):
            if w.lower() not in ("texas", "gov", "lt"):
                out.add(w.lower())
    return out


def is_poll_claim(c: dict[str, Any]) -> bool:
    return (c.get("type") == "fact" and c.get("checkable", True) and int(c.get("views") or 0) >= POLL_MIN_VIEWS
            and bool(POLL_RE.search(c.get("text") or "")) and bool(NUMBERS_RE.search(c.get("text") or "")))


def _label(members: list[dict[str, Any]]) -> str:
    """Distinct pollster names by views, skipping spellings that only re-use another's tokens."""
    by_name: dict[str, int] = defaultdict(int)
    for h in members:
        by_name[h["pollster"]] += h["views"]
    names, seen = [], []
    for n in sorted(by_name, key=lambda n: (-len(tokens(n)), -by_name[n], n)):  # most specific spelling first
        t = tokens(n)
        if any(t <= s_ or s_ <= t for s_ in seen):
            continue
        names.append(n); seen.append(t)
    names = names[:3]
    return ", ".join(names) + (" polls" if len(names) > 1 else " poll")


def events_from_claims(claims_by_tracker: dict[str, list[dict[str, Any]]], subjects: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    """claims_by_tracker: slug -> claims.json clusters; subjects: slug -> names the tracker is about (a poll claim
    counts for a tracker only when it mentions one, so an Ohio poll quoted in a Texas thread is not a Texas
    event). Returns events in load_events' shape plus auto/trackers/detail/views."""
    hits: list[dict[str, Any]] = []
    for slug, clusters in claims_by_tracker.items():
        subj = (subjects or {}).get(slug)
        for c in clusters:
            if not is_poll_claim(c):
                continue
            name = pollster(c["text"])
            if not name or not c.get("first_seen"):
                continue
            if subj and not any(s_ in c["text"].lower() for s_ in subj):
                continue
            hits.append({"slug": slug, "pollster": name, "tokens": tokens(name), "at": c["first_seen"], "views": int(c["views"]), "text": c["text"],
                         "handle": c.get("first_handle") or ""})
    hits.sort(key=lambda h: h["at"])
    groups: list[list[dict[str, Any]]] = []
    for h in hits:                                           # a release: claims sharing a pollster token within MERGE_H of the group's last claim
        t = datetime.fromisoformat(h["at"])
        matches = [g for g in groups if t - datetime.fromisoformat(g[-1]["at"]) <= timedelta(hours=MERGE_H)
                   and any(h["tokens"] & m["tokens"] for m in g)]
        if not matches:
            groups.append([h])
            continue
        home = matches[0]                                    # a claim that bridges two spellings joins the groups (UH/Hobby + University of Houston)
        for g in matches[1:]:
            home.extend(g)
            groups.remove(g)
        home.append(h)
        home.sort(key=lambda m: m["at"])
    out = []
    for members in groups:
        first = members[0]
        top = max(members, key=lambda h: h["views"])
        out.append({"label": _label(members), "type": "poll", "at": first["at"], "all_day": False,
                    "source": f"first post on X (@{first['handle']}), from {len(members)} claim{'s' if len(members) > 1 else ''}",
                    "auto": True, "trackers": sorted({h["slug"] for h in members}),
                    "detail": top["text"][:160], "views": sum(h["views"] for h in members)})
    out.sort(key=lambda e: e["at"])
    return out


@lru_cache(maxsize=1)
def auto_events() -> tuple[dict[str, Any], ...]:
    """Poll events from every tracker's claims.json (computed once per process)."""
    from .config import incident_paths, load_incident
    subjects: dict[str, set[str]] = {}
    for path in incident_paths():
        try:
            inc = load_incident(path)
            subjects[inc.slug] = subject_tokens(inc.queries)
        except Exception:                                    # a malformed incident file is not this module's problem
            continue
    by_tracker: dict[str, list[dict[str, Any]]] = {}
    if OUT_DIR.exists():
        for d in sorted(OUT_DIR.iterdir()):
            cp = d / "claims.json"
            if not d.is_dir() or not cp.exists():
                continue
            try:
                cj = json.loads(cp.read_text(encoding="utf-8"))
            except ValueError:
                continue
            by_tracker[d.name] = cj.get("clusters") or []
    return tuple(events_from_claims(by_tracker, subjects))


def covered_by(auto: dict[str, Any], hand: list[dict[str, Any]], hours: int = 24) -> bool:
    """A hand-entered poll event within `hours` whose label names the same pollster covers the automatic one."""
    toks = tokens(auto["label"])
    t = datetime.fromisoformat(auto["at"])
    for e in hand:
        if e.get("type") != "poll" or e.get("auto") or abs((datetime.fromisoformat(e["at"]) - t).total_seconds()) > hours * 3600:
            continue
        words = set(re.split(r"[^a-z0-9]+", e["label"].lower()))
        squashed = re.sub(r"[^a-z0-9]", "", e["label"].lower())
        if any(tok in words or (len(tok) >= 6 and tok in squashed) for tok in toks):
            return True
    return False
