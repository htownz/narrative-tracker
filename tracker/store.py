"""SQLite store for raw posts and derived labels. One file per incident."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    created_at TEXT,          -- ISO 8601 UTC
    view_count INTEGER,
    like_count INTEGER,
    retweet_count INTEGER,
    reply_count INTEGER,
    quote_count INTEGER,
    author_handle TEXT,
    author_name TEXT,
    author_avatar TEXT,
    author_followers INTEGER,
    text TEXT,
    url TEXT,
    in_reply_to_id TEXT,
    quoted_id TEXT,
    retweeted_id TEXT,
    in_dataset INTEGER DEFAULT 1,   -- 1: matched the search + threshold; 0: fetched only as a reference target
    lane TEXT,                      -- lane key, 'pushback', 'other', or NULL
    lane_source TEXT,               -- 'claude' | 'keywords' | 'manual'
    copy_origin TEXT,               -- id of the earliest post with the same words, or NULL
    raw TEXT                        -- original JSON from the API
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_lane ON posts(lane);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def parse_created_at(value: Any) -> str | None:
    """twitterapi.io returns Twitter's classic format: 'Tue Oct 13 02:15:00 +0000 2026'. Accept ISO too."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    s = str(value).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def flatten_tweet(t: dict[str, Any], in_dataset: int = 1) -> dict[str, Any]:
    """Map a twitterapi.io tweet object onto the posts table columns. Everything is .get() — the shape drifts."""
    author = t.get("author") or {}
    quoted = t.get("quoted_tweet") or {}
    retweeted = t.get("retweeted_tweet") or {}
    return {
        "id": str(t.get("id")),
        "created_at": parse_created_at(t.get("createdAt") or t.get("created_at")),
        "view_count": int(t.get("viewCount") or 0),
        "like_count": int(t.get("likeCount") or 0),
        "retweet_count": int(t.get("retweetCount") or 0),
        "reply_count": int(t.get("replyCount") or 0),
        "quote_count": int(t.get("quoteCount") or 0),
        "author_handle": author.get("userName") or author.get("screen_name") or "",
        "author_name": author.get("name") or "",
        "author_avatar": author.get("profilePicture") or "",
        "author_followers": int(author.get("followers") or 0),
        "text": t.get("text") or "",
        "url": t.get("url") or (f"https://x.com/{author.get('userName')}/status/{t.get('id')}" if author.get("userName") else ""),
        "in_reply_to_id": str(t["inReplyToId"]) if t.get("inReplyToId") else None,
        "quoted_id": str(quoted["id"]) if quoted.get("id") else None,
        "retweeted_id": str(retweeted["id"]) if retweeted.get("id") else None,
        "in_dataset": in_dataset,
        "raw": json.dumps(t, ensure_ascii=False),
    }


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Single-writer workload; MEMORY journal keeps SQLite working on FUSE/SMB-mounted folders
        # that refuse the rollback-journal locks (shows up as "disk I/O error").
        self.conn.execute("PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.executescript(SCHEMA)

    # ---- writes -------------------------------------------------------------
    def upsert_posts(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert or refresh posts. A row already in the dataset is never demoted to a reference-only row."""
        n = 0
        for r in rows:
            existing = self.conn.execute("SELECT in_dataset FROM posts WHERE id = ?", (r["id"],)).fetchone()
            if existing:
                r = dict(r)
                r["in_dataset"] = max(int(existing["in_dataset"] or 0), int(r.get("in_dataset", 0)))
                self.conn.execute(
                    """UPDATE posts SET created_at=:created_at, view_count=:view_count, like_count=:like_count,
                       retweet_count=:retweet_count, reply_count=:reply_count, quote_count=:quote_count,
                       author_handle=:author_handle, author_name=:author_name, author_avatar=:author_avatar,
                       author_followers=:author_followers, text=:text, url=:url, in_reply_to_id=:in_reply_to_id,
                       quoted_id=:quoted_id, retweeted_id=:retweeted_id, in_dataset=:in_dataset, raw=:raw
                       WHERE id=:id""",
                    r,
                )
            else:
                self.conn.execute(
                    """INSERT INTO posts (id, created_at, view_count, like_count, retweet_count, reply_count, quote_count,
                       author_handle, author_name, author_avatar, author_followers, text, url, in_reply_to_id,
                       quoted_id, retweeted_id, in_dataset, raw)
                       VALUES (:id, :created_at, :view_count, :like_count, :retweet_count, :reply_count, :quote_count,
                       :author_handle, :author_name, :author_avatar, :author_followers, :text, :url, :in_reply_to_id,
                       :quoted_id, :retweeted_id, :in_dataset, :raw)""",
                    r,
                )
            n += 1
        self.conn.commit()
        return n

    def set_lane(self, post_id: str, lane: str, source: str) -> None:
        self.conn.execute("UPDATE posts SET lane=?, lane_source=? WHERE id=?", (lane, source, post_id))

    def set_lanes(self, assignments: dict[str, str], source: str) -> None:
        self.conn.executemany(
            "UPDATE posts SET lane=?, lane_source=? WHERE id=?",
            [(lane, source, pid) for pid, lane in assignments.items()],
        )
        self.conn.commit()

    def set_copy_origins(self, origins: dict[str, str | None]) -> None:
        self.conn.execute("UPDATE posts SET copy_origin=NULL")
        self.conn.executemany("UPDATE posts SET copy_origin=? WHERE id=?", [(o, pid) for pid, o in origins.items()])
        self.conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    # ---- reads --------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def ids(self) -> set[str]:
        return {r["id"] for r in self.conn.execute("SELECT id FROM posts")}

    def dataset(self, min_views: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE in_dataset=1 AND view_count >= ? AND created_at IS NOT NULL ORDER BY created_at",
            (min_views,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_posts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM posts ORDER BY created_at")]

    def unlabeled(self, min_views: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE in_dataset=1 AND view_count >= ? AND lane IS NULL ORDER BY view_count DESC",
            (min_views,),
        ).fetchall()
        return [dict(r) for r in rows]

    def unlabeled_refs(self, min_views: int, window_start_utc: str, include_labeled: bool = False) -> list[dict[str, Any]]:
        """Reference-only posts (quoted / replied-to) that sit inside the window and clear the threshold —
        they get drawn, so they need a lane too."""
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE in_dataset=0 AND view_count >= ? AND created_at >= ? "
            + ("" if include_labeled else "AND lane IS NULL ") + "ORDER BY view_count DESC",
            (min_views, window_start_utc),
        ).fetchall()
        return [dict(r) for r in rows]

    def missing_reference_ids(self) -> set[str]:
        have = self.ids()
        wanted: set[str] = set()
        for r in self.conn.execute("SELECT in_reply_to_id, quoted_id, retweeted_id FROM posts WHERE in_dataset=1"):
            for col in ("in_reply_to_id", "quoted_id", "retweeted_id"):
                if r[col] and r[col] not in have:
                    wanted.add(r[col])
        return wanted

    def count(self, where: str = "1=1") -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
