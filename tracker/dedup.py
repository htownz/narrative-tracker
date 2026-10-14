"""Near-duplicate detection: 'same words' clusters.

Two posts are the same words when at least `similarity` (default 0.90) of the shorter post's words appear
in the other (word-multiset containment), after normalising away URLs, @mentions, case and punctuation —
the original chart's "90% or more the same words" rule. Word shingles only propose candidate pairs.
Each cluster's origin is the earliest post; every other member gets copy_origin = origin id.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .config import Incident
from .store import Store

URL_RE = re.compile(r"https?://\S+|t\.co/\S+")
MENTION_RE = re.compile(r"@\w+")
NONWORD_RE = re.compile(r"[^a-z0-9' ]+")


def normalise(text: str) -> list[str]:
    t = (text or "").lower()
    t = URL_RE.sub(" ", t)
    t = MENTION_RE.sub(" ", t)
    t = t.replace("&amp;", "&")
    t = NONWORD_RE.sub(" ", t)
    t = re.sub(r"^(rt|qt)\s+", "", t)
    return t.split()


def shingles(words: list[str], k: int) -> set[str]:
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def containment(a: Counter, b: Counter, min_len_ratio: float = 0.5) -> float:
    """Share of the shorter post's words (with multiplicity) that the other post also has.
    A short post swallowed by a much longer one is a quote, not a copy: when the shorter is under
    `min_len_ratio` of the longer, score 0."""
    if not a or not b:
        return 0.0
    la, lb = sum(a.values()), sum(b.values())
    if min(la, lb) < min_len_ratio * max(la, lb):
        return 0.0
    inter = sum((a & b).values())
    return inter / min(la, lb)


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def find_copies(posts: list[dict[str, Any]], k: int = 3, threshold: float = 0.90,
                min_words: int = 4) -> dict[str, str | None]:
    """Return {post_id: origin_id or None}. Posts shorter than min_words never cluster (too generic)."""
    sh: dict[str, set[str]] = {}
    bag: dict[str, Counter] = {}
    for p in posts:
        words = normalise(p["text"])
        if len(words) >= min_words:
            sh[p["id"]] = shingles(words, k)
            bag[p["id"]] = Counter(words)

    # inverted index on shingles -> candidate pairs only (avoids the full N^2)
    index: dict[str, list[str]] = defaultdict(list)
    for pid, s in sh.items():
        for g in s:
            index[g].append(pid)

    uf = UnionFind()
    checked: set[tuple[str, str]] = set()
    for g, members in index.items():
        if len(members) < 2 or len(members) > 400:   # a 3-gram shared by 400+ posts is boilerplate, not a copy
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                if containment(bag[a], bag[b]) >= threshold:
                    uf.union(a, b)

    by_time = {p["id"]: p["created_at"] or "" for p in posts}
    clusters: dict[str, list[str]] = defaultdict(list)
    for pid in sh:
        clusters[uf.find(pid)].append(pid)

    origins: dict[str, str | None] = {p["id"]: None for p in posts}
    for members in clusters.values():
        if len(members) < 2:
            continue
        origin = min(members, key=lambda m: by_time[m])
        for m in members:
            origins[m] = None if m == origin else origin
    return origins


def dedup(inc: Incident) -> None:
    """Same-words rings over every dataset post, not only the ones above the view threshold: a post that
    repeats a drawn post is drawn too (that is the chart's rule), whatever its own views. A ring counts
    when at least one member clears the threshold; its sub-threshold members inherit the ring's lane."""
    store = Store(inc.db_path)
    posts = store.dataset(0)
    k = int(inc.dedup.get("shingle_size", 3))
    thr = float(inc.dedup.get("similarity", 0.90))
    origins = find_copies(posts, k=k, threshold=thr)
    by_id = {p["id"]: p for p in posts}
    members: dict[str, list[str]] = {}
    for pid, o in origins.items():
        if o:
            members.setdefault(o, []).append(pid)
    keep: dict[str, str | None] = {p["id"]: None for p in posts}
    inherited: dict[str, str] = {}
    for o, ms in members.items():
        ring = [o] + ms
        if max(int(by_id[x]["view_count"] or 0) for x in ring) < inc.min_views:
            continue                                   # a ring of small posts only: not on the chart
        for m in ms:
            keep[m] = o
        # lane for members under the threshold (never classified): the origin's lane, else the biggest labelled member's
        labelled = [by_id[x] for x in ring if by_id[x]["lane"] and by_id[x]["lane_source"] != "copy"]
        if labelled:
            src = by_id[o] if by_id[o]["lane"] and by_id[o]["lane_source"] != "copy" else max(labelled, key=lambda q: int(q["view_count"] or 0))
            for x in ring:
                if int(by_id[x]["view_count"] or 0) < inc.min_views and by_id[x]["lane_source"] in (None, "copy"):
                    inherited[x] = src["lane"]
    store.set_copy_origins(keep)
    store.conn.execute("UPDATE posts SET lane=NULL, lane_source=NULL WHERE lane_source='copy'")   # re-derived every run
    if inherited:
        store.set_lanes(inherited, "copy")
    n_copies = sum(1 for v in keep.values() if v)
    n_clusters = len({v for v in keep.values() if v})
    print(f"same-words: {n_copies} repeated posts across {n_clusters} rings (word containment >= {thr}, {k}-gram candidates); "
          f"{len(inherited)} under {inc.min_views} views drawn with the ring's lane")
    store.close()
