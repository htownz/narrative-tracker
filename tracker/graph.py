"""Edges between posts: quote, reply, repost (from the API) and copy (from dedup)."""
from __future__ import annotations

from typing import Any


def build_edges(posts: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """posts: id -> row. Only edges whose both ends are in the chart are returned."""
    edges: list[dict[str, str]] = []
    for pid, p in posts.items():
        for col, kind in (("quoted_id", "quote"), ("in_reply_to_id", "reply"), ("retweeted_id", "repost")):
            target = p.get(col)
            if target and target in posts and target != pid:
                edges.append({"src": pid, "dst": target, "type": kind})
        origin = p.get("copy_origin")
        if origin and origin in posts and origin != pid:
            edges.append({"src": pid, "dst": origin, "type": "copy"})
    return edges
