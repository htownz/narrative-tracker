"""Near-duplicate detection: 'same words' clusters.

Two posts are the same words when the Jaccard similarity of their word-shingle sets is >= threshold
(default 0.90) after normalising away URLs, @mentions, case and punctuation. Each cluster's origin is
the earliest post; every other member gets copy_origin = origin id.
"""
from __future__ import annotations

import re
from collections import defaultdict
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
                min_words: int = 6) -> dict[str, str | None]:
    """Return {post_id: origin_id or None}. Posts shorter than min_words never cluster (too generic)."""
    sh: dict[str, set[str]] = {}
    for p in posts:
        words = normalise(p["text"])
        if len(words) >= min_words:
            sh[p["id"]] = shingles(words, k)

    # inverted index on shingles -> candidate pairs only (avoids the full N^2)
    index: dict[str, list[str]] = defaultdict(list)
    for pid, s in sh.items():
        for g in s:
            index[g].append(pid)

    uf = UnionFind()
    checked: set[tuple[str, str]] = set()
    for g, members in index.items():
        if len(members) < 2 or len(members) > 2000:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                if jaccard(sh[a], sh[b]) >= threshold:
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
    store = Store(inc.db_path)
    posts = store.dataset(inc.min_views)
    k = int(inc.dedup.get("shingle_size", 3))
    thr = float(inc.dedup.get("similarity", 0.90))
    origins = find_copies(posts, k=k, threshold=thr)
    store.set_copy_origins(origins)
    n_copies = sum(1 for v in origins.values() if v)
    n_clusters = len({v for v in origins.values() if v})
    print(f"same-words: {n_copies} repeated posts across {n_clusters} clusters (k={k}, jaccard>={thr})")
    store.close()
