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
    raw TEXT,                       -- original JSON from the API
    source TEXT DEFAULT 'x',        -- 'x' | 'bsky' | 'yt': the platform the post lives on
    engagement INTEGER DEFAULT 0    -- likes + reposts + replies + quotes; the threshold for platforms without view counts
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_lane ON posts(lane);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- view counts over time: one row per sighting of a post at the threshold (every re-pull and refresh), so growth
-- is measured rather than reconstructed (tracker/features.py, the weekly brief, the collapse-after-denial outcome)
CREATE TABLE IF NOT EXISTS view_history (
    post_id TEXT NOT NULL,
    at TEXT NOT NULL,
    view_count INTEGER,
    like_count INTEGER,
    retweet_count INTEGER,
    reply_count INTEGER,
    quote_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vh_post ON view_history(post_id, at);
-- claims layer (tracker/claims.py): what each post asserts, grouped into canonical claims
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    text TEXT NOT NULL,
    type TEXT,                -- fact | opinion | prediction | call_to_action
    checkable INTEGER DEFAULT 0,
    stance TEXT,              -- for | against | neutral (toward the subject)
    entities TEXT,            -- JSON list of proper nouns
    cluster_id INTEGER,       -- claim_clusters.id, NULL until clustered
    extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_post ON claims(post_id);
CREATE INDEX IF NOT EXISTS idx_claims_cluster ON claims(cluster_id);
CREATE TABLE IF NOT EXISTS claims_done (   -- posts that went through extraction, including ones with no claim
    post_id TEXT PRIMARY KEY,
    n INTEGER,
    at TEXT
);
CREATE TABLE IF NOT EXISTS claim_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,       -- canonical statement
    type TEXT,
    checkable INTEGER DEFAULT 0,
    stance TEXT,
    entities TEXT,
    verdict TEXT,             -- supported | unsupported | misleading | unverifiable (verification phase)
    verdict_basis TEXT,
    verdict_sources TEXT,     -- JSON list of URLs
    verified_at TEXT,
    created_at TEXT
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


# What of the API object is worth keeping in `raw`. The full object averages 4-5 KB per post (nested quoted
# tweets, cards, media entities, the author's bio and banner), which put the Paxton store past GitHub's 100 MB
# file limit; this subset is about 0.5 KB and holds everything the analysis reads (account age, follower
# counts, verification, language, reply/quote structure, links).
RAW_TWEET_KEYS = ("id", "createdAt", "lang", "source", "type", "isReply", "inReplyToId", "inReplyToUserId", "inReplyToUsername",
                  "conversationId", "bookmarkCount", "viewCount", "likeCount", "retweetCount", "replyCount", "quoteCount", "twitterUrl")
RAW_AUTHOR_KEYS = ("id", "userName", "name", "createdAt", "followers", "following", "statusesCount", "favouritesCount", "mediaCount",
                   "isBlueVerified", "isVerified", "verifiedType", "isAutomated", "automatedBy", "location", "description", "profilePicture")


def slim_raw(t: dict[str, Any]) -> dict[str, Any]:
    """The part of a twitterapi.io tweet object we keep. Idempotent (a slimmed object slims to itself)."""
    out: dict[str, Any] = {k: t[k] for k in RAW_TWEET_KEYS if k in t}
    a = t.get("author") or {}
    out["author"] = {k: a[k] for k in RAW_AUTHOR_KEYS if k in a}
    if isinstance(out["author"].get("description"), str):
        out["author"]["description"] = out["author"]["description"][:200]
    ents = t.get("entities") or {}
    urls = [u.get("expanded_url") or u.get("url") for u in (ents.get("urls") or []) if isinstance(u, dict)]
    tags = [h.get("text") for h in (ents.get("hashtags") or []) if isinstance(h, dict)]
    if urls or tags:
        out["entities"] = {"urls": [u for u in urls if u][:5], "hashtags": [h for h in tags if h][:10]}
    media = t.get("extendedEntities") or {}
    kinds = [m.get("type") for m in (media.get("media") or []) if isinstance(m, dict)]
    if kinds:
        out["media"] = kinds[:4]
    for key in ("quoted_tweet", "retweeted_tweet"):
        q = t.get(key)
        if isinstance(q, dict) and q.get("id"):
            qa = q.get("author")
            out[key] = {"id": str(q["id"]), "author": qa.get("userName") if isinstance(qa, dict) else qa}
    if t.get("synthetic"):
        out["synthetic"] = True
    if t.get("media") and "media" not in out:        # already-slim object
        out["media"] = t["media"]
    if t.get("entities") and "entities" not in out and "urls" in t["entities"] and isinstance(t["entities"]["urls"], list) and all(isinstance(u, str) for u in t["entities"]["urls"]):
        out["entities"] = t["entities"]
    out["_slim"] = 1
    return out


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
        "raw": json.dumps(slim_raw(t), ensure_ascii=False),
        "source": "x",
        "engagement": int(t.get("likeCount") or 0) + int(t.get("retweetCount") or 0) + int(t.get("replyCount") or 0) + int(t.get("quoteCount") or 0),
    }


SOURCES = {"x": "X", "bsky": "Bluesky", "yt": "YouTube", "truth": "Truth Social"}
KNOWN_PROFILES = {"realDonaldTrump": "https://truthsocial.com/@realDonaldTrump"}
BSKY_MIN_ENGAGEMENT = 25      # Bluesky has no view counts; a post with this much engagement is roughly a 1K-view post on X


def profile_url(handle: str) -> str:
    """Where an account lives. Bluesky handles are domains (name.bsky.social); YouTube channels are stored as yt:<id>."""
    if handle in KNOWN_PROFILES:
        return KNOWN_PROFILES[handle]
    if handle.startswith("yt:"):
        return f"https://www.youtube.com/channel/{handle[3:]}"
    if "." in handle:
        return f"https://bsky.app/profile/{handle}"
    return f"https://x.com/{handle}"


def flatten_bsky(p: dict[str, Any], in_dataset: int = 1) -> dict[str, Any]:
    """Map a Bluesky PostView (app.bsky.feed.searchPosts / getPosts) onto the posts table. The post id is its
    at:// URI. No view counts exist on Bluesky: view_count stays 0 and the engagement column carries the threshold."""
    author = p.get("author") or {}
    rec = p.get("record") or {}
    uri = str(p.get("uri") or "")
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    handle = author.get("handle") or ""
    embed = p.get("embed") or {}
    quoted = None
    if "record" in embed:                                    # app.bsky.embed.record#view or recordWithMedia#view
        q = embed["record"]
        q = q.get("record") if isinstance(q.get("record"), dict) and "uri" not in q else q
        if isinstance(q, dict) and q.get("uri"):
            quoted = {"id": str(q["uri"]), "author": ((q.get("author") or {}).get("handle") or ""), "text": (q.get("value") or {}).get("text", "")}
    reply = (rec.get("reply") or {}).get("parent") or {}
    urls = []
    for f in rec.get("facets") or []:
        for feat in f.get("features") or []:
            if feat.get("uri"):
                urls.append(feat["uri"])
    ext = embed.get("external") or (embed.get("media") or {}).get("external") or {}
    if ext.get("uri"):
        urls.append(ext["uri"])
    likes, reposts, replies, quotes = (int(p.get(k) or 0) for k in ("likeCount", "repostCount", "replyCount", "quoteCount"))
    raw = {"_slim": 1, "_src": "bsky", "id": uri, "createdAt": rec.get("createdAt"), "lang": (rec.get("langs") or [None])[0],
           "author": {"id": author.get("did"), "userName": handle, "name": author.get("displayName"), "createdAt": author.get("createdAt"),
                      "followers": int(author.get("followersCount") or 0), "profilePicture": author.get("avatar"), "description": (author.get("description") or "")[:200]},
           "entities": {"urls": urls[:10], "hashtags": []}, "quoted_tweet": {"id": quoted["id"], "author": quoted["author"]} if quoted else None,
           "media": ["image"] if "images" in embed or "images" in (embed.get("media") or {}) else (["video"] if "playlist" in embed or "playlist" in (embed.get("media") or {}) else [])}
    return {
        "id": uri, "created_at": parse_created_at(rec.get("createdAt") or p.get("indexedAt")),
        "view_count": 0, "like_count": likes, "retweet_count": reposts, "reply_count": replies, "quote_count": quotes,
        "author_handle": handle, "author_name": author.get("displayName") or "", "author_avatar": author.get("avatar") or "",
        "author_followers": int(author.get("followersCount") or 0),
        "text": rec.get("text") or "", "url": f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else "",
        "in_reply_to_id": str(reply["uri"]) if reply.get("uri") else None,
        "quoted_id": quoted["id"] if quoted else None, "retweeted_id": None,
        "in_dataset": in_dataset, "raw": json.dumps(raw, ensure_ascii=False),
        "source": "bsky", "engagement": likes + reposts + replies + quotes,
    }


def _thr(min_views: int, min_engagement: int) -> tuple[str, tuple[int, int]]:
    """SQL for 'this post clears its platform's threshold'."""
    return "(view_count >= ? OR (source = 'bsky' AND engagement >= ?) OR source = 'truth')", (min_views, min_engagement)


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
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "source" not in cols:                              # stores created before posts had a platform
            self.conn.execute("ALTER TABLE posts ADD COLUMN source TEXT DEFAULT 'x'")
        if "engagement" not in cols:
            self.conn.execute("ALTER TABLE posts ADD COLUMN engagement INTEGER DEFAULT 0")
            self.conn.execute("UPDATE posts SET engagement = COALESCE(like_count,0)+COALESCE(retweet_count,0)+COALESCE(reply_count,0)+COALESCE(quote_count,0)")
        self.conn.commit()

    # ---- writes -------------------------------------------------------------
    HISTORY_MIN_VIEWS = 1000

    def record_views(self, rows: Iterable[dict[str, Any]], at: str | None = None) -> int:
        """Append a view_history sighting for each post at the threshold, unless the latest sighting already
        shows the same count (re-pulls of a quiet post add nothing)."""
        at = at or datetime.now(timezone.utc).isoformat(timespec="minutes")
        n = 0
        for r in rows:
            v = int(r.get("view_count") or 0)
            if v < self.HISTORY_MIN_VIEWS:
                continue
            last = self.conn.execute("SELECT view_count FROM view_history WHERE post_id=? ORDER BY at DESC LIMIT 1", (r["id"],)).fetchone()
            if last and int(last["view_count"] or 0) == v:
                continue
            self.conn.execute("INSERT INTO view_history (post_id, at, view_count, like_count, retweet_count, reply_count, quote_count) VALUES (?,?,?,?,?,?,?)",
                              (r["id"], at, v, int(r.get("like_count") or 0), int(r.get("retweet_count") or 0), int(r.get("reply_count") or 0), int(r.get("quote_count") or 0)))
            n += 1
        return n

    def seed_history(self) -> int:
        """First run with the history table: one sighting per post at the threshold, dated by the last pull."""
        if self.conn.execute("SELECT 1 FROM view_history LIMIT 1").fetchone():
            return 0
        at = self.get_meta("pulled_at") or datetime.now(timezone.utc).isoformat(timespec="minutes")
        rows = [dict(r) for r in self.conn.execute("SELECT id, view_count, like_count, retweet_count, reply_count, quote_count FROM posts WHERE view_count >= ?",
                                                   (self.HISTORY_MIN_VIEWS,))]
        n = self.record_views(rows, at=at)
        self.conn.commit()
        return n

    def view_series(self, post_ids: Iterable[str]) -> dict[str, list[tuple[str, int]]]:
        out: dict[str, list[tuple[str, int]]] = {}
        ids = list(post_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT post_id, at, view_count FROM view_history WHERE post_id IN ({','.join('?' * len(chunk))}) ORDER BY post_id, at"
            for r in self.conn.execute(q, chunk):
                out.setdefault(r["post_id"], []).append((r["at"], int(r["view_count"] or 0)))
        return out

    def upsert_posts(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert or refresh posts. A row already in the dataset is never demoted to a reference-only row.
        Every sighting at the threshold is also appended to view_history."""
        rows = list(rows)
        self.record_views(rows)
        n = 0
        for r in rows:
            existing = self.conn.execute("SELECT in_dataset FROM posts WHERE id = ?", (r["id"],)).fetchone()
            r = dict(r)
            r.setdefault("source", "x")
            r.setdefault("engagement", int(r.get("like_count") or 0) + int(r.get("retweet_count") or 0) + int(r.get("reply_count") or 0) + int(r.get("quote_count") or 0))
            if existing:
                r["in_dataset"] = max(int(existing["in_dataset"] or 0), int(r.get("in_dataset", 0)))
                self.conn.execute(
                    """UPDATE posts SET created_at=:created_at, view_count=:view_count, like_count=:like_count,
                       retweet_count=:retweet_count, reply_count=:reply_count, quote_count=:quote_count,
                       author_handle=:author_handle, author_name=:author_name, author_avatar=:author_avatar,
                       author_followers=:author_followers, text=:text, url=:url, in_reply_to_id=:in_reply_to_id,
                       quoted_id=:quoted_id, retweeted_id=:retweeted_id, in_dataset=:in_dataset, raw=:raw,
                       source=:source, engagement=:engagement
                       WHERE id=:id""",
                    r,
                )
            else:
                self.conn.execute(
                    """INSERT INTO posts (id, created_at, view_count, like_count, retweet_count, reply_count, quote_count,
                       author_handle, author_name, author_avatar, author_followers, text, url, in_reply_to_id,
                       quoted_id, retweeted_id, in_dataset, raw, source, engagement)
                       VALUES (:id, :created_at, :view_count, :like_count, :retweet_count, :reply_count, :quote_count,
                       :author_handle, :author_name, :author_avatar, :author_followers, :text, :url, :in_reply_to_id,
                       :quoted_id, :retweeted_id, :in_dataset, :raw, :source, :engagement)""",
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

    def dataset(self, min_views: int = 0, min_engagement: int = BSKY_MIN_ENGAGEMENT) -> list[dict[str, Any]]:
        thr, args = _thr(min_views, min_engagement)
        rows = self.conn.execute(
            f"SELECT * FROM posts WHERE in_dataset=1 AND {thr} AND created_at IS NOT NULL ORDER BY created_at", args,
        ).fetchall()
        return [dict(r) for r in rows]

    def drawable(self, min_views: int = 0, min_engagement: int = BSKY_MIN_ENGAGEMENT) -> list[dict[str, Any]]:
        """What the chart shows: dataset posts at the threshold, plus every dataset post that repeats one (any views)."""
        thr, args = _thr(min_views, min_engagement)
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE in_dataset=1 AND created_at IS NOT NULL AND "
            f"({thr} OR (copy_origin IS NOT NULL AND lane IS NOT NULL)) ORDER BY created_at", args,
        ).fetchall()
        return [dict(r) for r in rows]

    def chart_posts(self, min_views: int, window_start_utc: str, min_engagement: int = BSKY_MIN_ENGAGEMENT) -> list[dict[str, Any]]:
        """Everything the chart draws: drawable dataset posts plus labelled reference posts (quoted / replied-to)
        inside the window at the threshold — the same rule export.py uses."""
        thr, args = _thr(min_views, min_engagement)
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE created_at IS NOT NULL AND ("
            f"(in_dataset=1 AND ({thr} OR (copy_origin IS NOT NULL AND lane IS NOT NULL))) OR "
            f"(in_dataset=0 AND {thr} AND created_at >= ? AND lane IS NOT NULL)) ORDER BY created_at",
            args + args + (window_start_utc,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_posts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM posts ORDER BY created_at")]

    def unlabeled(self, min_views: int = 0, min_engagement: int = BSKY_MIN_ENGAGEMENT) -> list[dict[str, Any]]:
        thr, args = _thr(min_views, min_engagement)
        rows = self.conn.execute(
            f"SELECT * FROM posts WHERE in_dataset=1 AND {thr} AND lane IS NULL ORDER BY view_count DESC, engagement DESC", args,
        ).fetchall()
        return [dict(r) for r in rows]

    def unlabeled_refs(self, min_views: int, window_start_utc: str, include_labeled: bool = False,
                       min_engagement: int = BSKY_MIN_ENGAGEMENT) -> list[dict[str, Any]]:
        """Reference-only posts (quoted / replied-to) that sit inside the window and clear the threshold —
        they get drawn, so they need a lane too."""
        thr, args = _thr(min_views, min_engagement)
        rows = self.conn.execute(
            f"SELECT * FROM posts WHERE in_dataset=0 AND {thr} AND created_at >= ? "
            + ("" if include_labeled else "AND lane IS NULL ") + "ORDER BY view_count DESC, engagement DESC",
            args + (window_start_utc,),
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

    # ---- claims ---------------------------------------------------------------
    def claims_pending(self, min_views: int, window_start_utc: str, limit: int, min_engagement: int = BSKY_MIN_ENGAGEMENT * 2) -> list[dict[str, Any]]:
        """In-lane posts (not 'other') that have not been through claim extraction, biggest first."""
        rows = self.conn.execute(
            "SELECT p.* FROM posts p LEFT JOIN claims_done d ON d.post_id = p.id "
            "WHERE d.post_id IS NULL AND p.lane IS NOT NULL AND p.lane != 'other' AND (p.view_count >= ? OR (p.source = 'bsky' AND p.engagement >= ?) OR p.source = 'truth') "
            "AND p.created_at >= ? ORDER BY p.view_count DESC, p.engagement DESC LIMIT ?",
            (min_views, min_engagement, window_start_utc, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_claims(self, post_id: str, claims: list[dict[str, Any]], at: str) -> None:
        self.conn.executemany(
            "INSERT INTO claims (post_id, text, type, checkable, stance, entities, extracted_at) VALUES (?,?,?,?,?,?,?)",
            [(post_id, c["text"], c.get("type"), int(bool(c.get("checkable"))), c.get("stance"),
              json.dumps(c.get("entities") or [], ensure_ascii=False), at) for c in claims],
        )
        self.conn.execute("INSERT OR REPLACE INTO claims_done (post_id, n, at) VALUES (?,?,?)", (post_id, len(claims), at))
        self.conn.commit()

    def unclustered_claims(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM claims WHERE cluster_id IS NULL ORDER BY id")]

    def claims_all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM claims ORDER BY id")]

    def clusters(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM claim_clusters ORDER BY id")]

    def add_cluster(self, text: str, type_: str | None, checkable: bool, stance: str | None, entities: list[str], at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO claim_clusters (text, type, checkable, stance, entities, created_at) VALUES (?,?,?,?,?,?)",
            (text, type_, int(bool(checkable)), stance, json.dumps(entities or [], ensure_ascii=False), at),
        )
        return int(cur.lastrowid)

    def assign_clusters(self, assignments: dict[int, int]) -> None:
        self.conn.executemany("UPDATE claims SET cluster_id=? WHERE id=?", [(cid, claim_id) for claim_id, cid in assignments.items()])
        self.conn.commit()

    def set_verdict(self, cluster_id: int, verdict: str, basis: str, sources: list[str], at: str) -> None:
        self.conn.execute("UPDATE claim_clusters SET verdict=?, verdict_basis=?, verdict_sources=?, verified_at=? WHERE id=?",
                          (verdict, basis, json.dumps(sources), at, cluster_id))
        self.conn.commit()

    def compact(self) -> int:
        """Slim every full-size raw object still in the store (rows written before slim_raw existed) and
        reclaim the space. Returns the number of rows rewritten. Safe to run every time; a no-op once done."""
        rows = self.conn.execute("SELECT id, raw FROM posts WHERE raw IS NOT NULL AND raw NOT LIKE '%\"_slim\": 1%'").fetchall()
        n = 0
        for r in rows:
            try:
                t = json.loads(r["raw"])
            except (TypeError, ValueError):
                continue
            self.conn.execute("UPDATE posts SET raw=? WHERE id=?", (json.dumps(slim_raw(t), ensure_ascii=False), r["id"]))
            n += 1
        if n:
            self.conn.commit()
            self.conn.execute("VACUUM")
        return n

    def count(self, where: str = "1=1") -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
