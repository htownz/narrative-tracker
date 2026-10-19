"""Lane discovery and classification.

discover(): read the top posts by views, propose 4-6 distinct narratives ("lanes").
classify(): put every in-dataset post into a lane, 'pushback', or 'other'.

Provider 'claude' uses the Anthropic API (ANTHROPIC_API_KEY). Provider 'keywords' is the
no-API fallback: regex keywords per lane, first match wins, pushback keywords checked first.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from .config import OTHER_KEY, PUSHBACK_KEY, Incident, env
from .store import Store

DISCOVER_SYSTEM = """You analyse how political stories spread on X. You will be shown the most-viewed posts
about a subject over a short window. Identify the distinct narratives being pushed — the separate versions
of the story, claims, or attack lines — not topics in general. Return 4 to 6 lanes. Each lane must be
something a post can be sorted into unambiguously. Do not include a pushback/skeptic lane; that exists
already. Respond with JSON only."""

DISCOVER_USER = """Subject: {subject}
Window: {start} to {end}
{hint}

Top posts (id | views | handle | text):
{posts}

Return JSON of the form:
{{"lanes": [{{"key": "short_snake_key", "label": "Short label, 3-6 words, as the chart row header",
             "description": "One sentence: what a post in this lane argues or claims.",
             "keywords": ["3-8 lowercase words or phrases typical of this lane"]}}, ...]}}
Order lanes from the one that appeared first to the one that appeared last."""

CLASSIFY_SYSTEM = """You sort posts about a political story into lanes. Each post gets exactly one lane key.
Use "pushback" for posts that doubt, mock, fact-check, defend the subject against, or argue with the
narratives. Use "other" only when the post is off-topic or fits no lane. Respond with JSON only."""

CLASSIFY_USER = """Subject: {subject}

Lanes:
{lanes}
pushback: {pushback}
other: off-topic or no fit
{examples}
Posts (id | handle | text):
{posts}

Return JSON: {{"assignments": {{"<post id>": "<lane key>", ...}}}} covering every post id listed."""

EXAMPLES_MAX = 20


def few_shot(inc: Incident, store: Store | None, valid: set[str]) -> str:
    """Human corrections (incidents/<slug>.overrides.csv) as labelled examples in the prompt, newest first,
    so a correction teaches the classifier instead of only patching one post."""
    if store is None:
        return ""
    from .review import load_overrides
    ov = [(pid, lane) for pid, lane in load_overrides(inc).items() if lane in valid]
    if not ov:
        return ""
    lines = []
    for pid, lane in reversed(ov[-EXAMPLES_MAX:]):
        row = store.conn.execute("SELECT author_handle, text FROM posts WHERE id=?", (pid,)).fetchone()
        if row and row["text"]:
            lines.append(f"@{row['author_handle']} | {_clean(row['text'], 200)} -> {lane}")
    if not lines:
        return ""
    return "\nExamples of correct labels from human review (handle | text -> lane):\n" + "\n".join(lines) + "\n"


def _client():
    try:
        import anthropic  # noqa: WPS433
    except ImportError:
        sys.exit("pip install anthropic   (or set classifier.provider: keywords)")
    key = env("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set (env var or .env)")
    return anthropic.Anthropic(api_key=key)


def _json_from(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def _ask(client, model: str, system: str, user: str, max_tokens: int = 4000, step: str = "ask", slug: str = "") -> dict[str, Any]:
    """One call, one retry on a non-JSON reply; {} when it still will not parse (callers treat {} as 'no result').
    Every reply's token usage is logged to data/usage.csv under `step` (tracker/usage.py)."""
    from .usage import record
    last_err = None
    for attempt in range(2):
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user + ("\n\nReply with valid JSON only." if attempt else "")}],
        )
        record(step, model, msg, slug)
        text = "".join(getattr(b, "text", "") for b in msg.content)
        try:
            return _json_from(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    print(f"  model reply was not JSON ({last_err}); treating as empty", file=sys.stderr)
    return {}


def _clean(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


# ---------------------------------------------------------------------------
def discover(inc: Incident, top_n: int = 150) -> list[dict[str, Any]]:
    store = Store(inc.db_path)
    posts = sorted(store.dataset(inc.min_views), key=lambda p: -p["view_count"])[:top_n]
    store.close()
    if not posts:
        sys.exit("no posts in the store yet — run pull first")
    model = inc.classifier.get("model", "claude-sonnet-5-5")
    listing = "\n".join(f"{p['id']} | {p['view_count']} | @{p['author_handle']} | {_clean(p['text'])}" for p in posts)
    hint = f"Guidance: {inc.discover_hint}\n" if inc.discover_hint else ""
    out = _ask(_client(), model, DISCOVER_SYSTEM,
               DISCOVER_USER.format(subject=inc.subject, start=inc.start, end=inc.end, posts=listing, hint=hint), step="discover", slug=inc.slug)
    lanes = out.get("lanes") or []
    cleaned = []
    for i, lane in enumerate(lanes):
        key = re.sub(r"[^a-z0-9_]+", "_", str(lane.get("key", f"lane_{i+1}")).lower()).strip("_") or f"lane_{i+1}"
        if key in (PUSHBACK_KEY, OTHER_KEY):
            key = f"{key}_narrative"
        cleaned.append({
            "key": key,
            "label": str(lane.get("label", key)).strip(),
            "description": str(lane.get("description", "")).strip(),
            "keywords": [str(k).lower() for k in (lane.get("keywords") or [])][:10],
        })
    return cleaned


def classify_keywords(inc: Incident, posts: list[dict[str, Any]]) -> dict[str, str]:
    lanes = inc.lane_table()
    pushback_kw = [kw for lane in lanes if lane["key"] == PUSHBACK_KEY for kw in lane["keywords"]]
    compiled = [(lane["key"], [re.compile(re.escape(k), re.I) for k in lane["keywords"]])
                for lane in lanes if lane["key"] != PUSHBACK_KEY]
    pb = [re.compile(re.escape(k), re.I) for k in pushback_kw]
    out = {}
    for p in posts:
        text = p["text"] or ""
        if any(r.search(text) for r in pb):
            out[p["id"]] = PUSHBACK_KEY
            continue
        hit = next((key for key, regs in compiled if any(r.search(text) for r in regs)), OTHER_KEY)
        out[p["id"]] = hit
    return out


def classify_claude(inc: Incident, posts: list[dict[str, Any]], store: Store | None = None) -> dict[str, str]:
    """Label posts in batches. When a store is given, each batch is committed as it returns, so an
    interrupted run (shell time caps, network blips) resumes with `classify` on the unlabeled remainder."""
    client = _client()
    model = inc.classifier.get("model", "claude-sonnet-5-5")
    batch = int(inc.classifier.get("batch_size", 40))
    lanes = [l for l in inc.lane_table() if l["key"] != PUSHBACK_KEY]
    lane_text = "\n".join(f"{l['key']}: {l['label']} — {l['description']}" for l in lanes)
    valid = {l["key"] for l in lanes} | {PUSHBACK_KEY, OTHER_KEY}
    examples = few_shot(inc, store, valid)
    if examples:
        print(f"  using {examples.count(chr(10) + '@')} human-reviewed examples in the prompt")
    out: dict[str, str] = {}
    for i in range(0, len(posts), batch):
        chunk = posts[i:i + batch]
        listing = "\n".join(f"{p['id']} | @{p['author_handle']} | {_clean(p['text'])}" for p in chunk)
        res = _ask(client, model, CLASSIFY_SYSTEM,
                   CLASSIFY_USER.format(subject=inc.subject, lanes=lane_text,
                                        pushback=inc.pushback.get("description", "doubting or mocking the story"),
                                        examples=examples, posts=listing), step="classify", slug=inc.slug)
        got = res.get("assignments") or {}
        part: dict[str, str] = {}
        for p in chunk:
            lane = str(got.get(p["id"], OTHER_KEY))
            part[p["id"]] = lane if lane in valid else OTHER_KEY
        out.update(part)
        if store is not None:
            store.set_lanes(part, "claude")
        print(f"  classified {min(i + batch, len(posts))}/{len(posts)}", flush=True)
    return out


def classify(inc: Incident, relabel: bool = False) -> None:
    if not inc.lanes:
        sys.exit("incident has no lanes yet — run `discover` (or write lanes into the YAML) first")
    store = Store(inc.db_path)
    posts = store.dataset(inc.min_views) if relabel else store.unlabeled(inc.min_views)
    # quoted / replied-to posts inside the window are drawn as link targets, so label them as well
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    window_start = datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()
    posts += store.unlabeled_refs(inc.min_views, window_start, include_labeled=relabel)
    if not posts:
        print("nothing to classify")
        from .review import apply_overrides
        apply_overrides(inc, store)
        store.close()
        return
    provider = inc.classifier.get("provider", "claude")
    if provider == "claude" and env("ANTHROPIC_API_KEY"):
        assignments = classify_claude(inc, posts, store=store)
        source = "claude"
    else:
        if provider == "claude":
            print("ANTHROPIC_API_KEY not set — falling back to keyword classifier", file=sys.stderr)
        assignments = classify_keywords(inc, posts)
        source = "keywords"
        store.set_lanes(assignments, source)
    counts: dict[str, int] = {}
    for lane in assignments.values():
        counts[lane] = counts.get(lane, 0) + 1
    print(f"labelled {len(assignments)} posts via {source}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    from .review import apply_overrides
    apply_overrides(inc, store)
    store.close()


def other_report(inc: Incident, top_n: int = 10) -> None:
    """Print the most-viewed posts that fit no lane — the signal that a new narrative needs a lane."""
    store = Store(inc.db_path)
    rows = store.conn.execute(
        "SELECT view_count, author_handle, created_at, text FROM posts WHERE in_dataset=1 AND view_count >= ? "
        "AND lane = ? ORDER BY view_count DESC LIMIT ?", (inc.min_views, OTHER_KEY, top_n)).fetchall()
    n_other = store.count(f"in_dataset=1 AND view_count >= {inc.min_views} AND lane = '{OTHER_KEY}'")
    n_all = store.count(f"in_dataset=1 AND view_count >= {inc.min_views} AND lane IS NOT NULL")
    store.close()
    print(f"'other' share: {n_other}/{n_all} labelled posts ({100 * n_other / max(1, n_all):.0f}%). "
          f"Top unlaned posts — if several share a story, add a lane to the YAML and run `classify --relabel`:")
    for v, h, t, text in rows:
        print(f"  {v:>8,}  @{h:<18} {t[:10]}  {_clean(text, 120)}")
