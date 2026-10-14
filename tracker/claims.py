"""Claims layer: what each post asserts, grouped into canonical claims.

Lanes say what a post argues; claims say what it asserts is true. Three steps, all incremental:

  extract()  Claude pulls 0-3 atomic claims out of every in-lane post (each post is sent once;
             `claims.per_run` most-viewed unprocessed posts per run keeps the cost bounded)
  cluster()  new claims are matched to existing canonical claims — lexically when the words
             nearly match, by Claude otherwise — or open new canonical claims
  claims()   extract + cluster + per-claim stats -> out/<slug>/claims.json and claims.html

YAML (all optional):
  claims:
    min_views: 2000     # posts below this are not sent for extraction (default max(min_views, 2000))
    per_run: 300        # unprocessed posts handled per run, biggest first
    batch_size: 25
    model: claude-sonnet-5-5   # default: classifier.model
"""
from __future__ import annotations

import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PUSHBACK_KEY, Incident, env
from .lanes import _ask, _clean, _client
from .store import Store

TYPES = {"fact", "opinion", "prediction", "call_to_action"}
STANCES = {"for", "against", "neutral"}

EXTRACT_SYSTEM = """You extract the claims made in posts about a political story. A claim is one atomic assertion
a reader is meant to take away: a statement of fact, an opinion, a prediction, or a call to action. Write each
claim as one short standalone sentence in plain words, naming people and things explicitly (never 'he', 'she',
'this', 'they'). Keep the claim as the post makes it, even when it is false. Skip bare links, jokes that assert
nothing, and pure emotion. Zero to three claims per post. Respond with JSON only."""

EXTRACT_USER = """Subject: {subject}

type: fact (asserts something about the world), opinion (a judgement or characterisation), prediction (what
will happen), call_to_action (vote, donate, share, resign, debate).
checkable: true when a diligent reporter could confirm or refute the claim with public records or reporting.
stance: "for" when the claim favours the subject's side, "against" when it attacks the subject's side, "neutral" otherwise.
entities: the proper nouns the claim is about (people, organisations, bills, places), 1-4 items.

Posts (id | handle | text):
{posts}

Return JSON:
{{"claims": {{"<post id>": [{{"text": "...", "type": "fact", "checkable": true, "stance": "against", "entities": ["..."]}}, ...], ...}}}}
Include every post id, with [] for a post that makes no claim."""

CLUSTER_SYSTEM = """You group claims about a political story into canonical claims. Two claims belong together when
they assert the same thing, even in different words or with different detail. Different assertions about the
same person stay separate. Respond with JSON only."""

CLUSTER_USER = """Subject: {subject}

Existing canonical claims (id | text):
{existing}

New claims (id | text):
{new}

For each new claim, assign it to an existing canonical claim id when it asserts the same thing, otherwise to a
new group. Name new groups g1, g2, ... and give each a canonical text: one short plain sentence stating the claim,
naming people explicitly. Return JSON:
{{"assign": {{"<new claim id>": "<existing id or g-key>", ...}},
  "groups": {{"g1": {{"text": "...", "type": "fact", "checkable": true, "stance": "against", "entities": ["..."]}}, ...}}}}
Cover every new claim id."""

STOP = set("""a an the and or but of to in on at for with by from as is are was were be been being this that these those
it its he she they them his her their we you i our your not no do does did have has had will would can could should
about into over than then so if just more most very who whom what which when where why how all any some such own same
says said say up out off down vs""".split())

LEXICAL_ASSIGN = 0.6     # a new claim this close to a canonical text joins it without asking the model
LEXICAL_GROUP = 0.7      # new claims this close to each other are sent to the model as one
EXISTING_TOP = 150       # most-viewed canonical claims always shown to the model
NEIGHBOURS = 3           # plus the closest canonical claims per new claim


def _settings(inc: Incident) -> dict[str, Any]:
    c = dict(inc.raw.get("claims") or {})
    return {
        "min_views": int(c.get("min_views", max(inc.min_views, 2000))),
        "per_run": int(c.get("per_run", 300)),
        "batch_size": int(c.get("batch_size", 25)),
        "model": c.get("model") or inc.classifier.get("model", "claude-sonnet-5-5"),
    }


def _window_start_utc(inc: Incident) -> str:
    return datetime.fromisoformat(inc.start).replace(tzinfo=ZoneInfo(inc.timezone)).astimezone(timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- lexical helpers -------------------------------------------------------------------------
def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9$%']+", (text or "").lower())
    return {w.strip("'") for w in words if w.strip("'") and w not in STOP and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _valid_claim(c: Any) -> dict[str, Any] | None:
    if not isinstance(c, dict) or not str(c.get("text") or "").strip():
        return None
    text = _clean(str(c["text"]), 240)
    if len(tokens(text)) < 2:
        return None
    t = str(c.get("type") or "opinion").lower().replace(" ", "_")
    s = str(c.get("stance") or "neutral").lower()
    ents = c.get("entities") or []
    if not isinstance(ents, list):
        ents = []
    return {"text": text, "type": t if t in TYPES else "opinion", "checkable": bool(c.get("checkable")),
            "stance": s if s in STANCES else "neutral", "entities": [str(e)[:60] for e in ents][:4]}


# ---- 1. extraction ----------------------------------------------------------------------------
def extract(inc: Incident, store: Store, client=None) -> int:
    st = _settings(inc)
    posts = store.claims_pending(st["min_views"], _window_start_utc(inc), st["per_run"])
    if not posts:
        print("claims: nothing new to extract")
        return 0
    client = client or _client()
    done = 0
    batch = st["batch_size"]
    for i in range(0, len(posts), batch):
        chunk = posts[i:i + batch]
        listing = "\n".join(f"{p['id']} | @{p['author_handle']} | {_clean(p['text'], 400)}" for p in chunk)
        res = _ask(client, st["model"], EXTRACT_SYSTEM, EXTRACT_USER.format(subject=inc.subject, posts=listing), max_tokens=8000)
        got = res.get("claims") if isinstance(res.get("claims"), dict) else None
        if got is None:
            print("  claims: batch returned nothing usable; will retry those posts next run", file=sys.stderr)
            continue
        at = _now()
        for p in chunk:
            raw = got.get(p["id"])
            if raw is None:          # the model skipped this id: leave it for the next run
                continue
            items = [c for c in (_valid_claim(x) for x in (raw if isinstance(raw, list) else [])) if c][:3]
            store.add_claims(p["id"], items, at)
            done += 1
        print(f"  claims extracted for {min(i + batch, len(posts))}/{len(posts)} posts", flush=True)
    return done


# ---- 2. clustering ---------------------------------------------------------------------------
def _cluster_views(store: Store) -> dict[int, int]:
    views: dict[int, int] = defaultdict(int)
    seen: set[tuple[int, str]] = set()
    rows = store.conn.execute("SELECT c.cluster_id, c.post_id, p.view_count FROM claims c JOIN posts p ON p.id = c.post_id "
                              "WHERE c.cluster_id IS NOT NULL").fetchall()
    for r in rows:
        key = (int(r["cluster_id"]), r["post_id"])
        if key in seen:
            continue
        seen.add(key)
        views[int(r["cluster_id"])] += int(r["view_count"] or 0)
    return views


def cluster(inc: Incident, store: Store, client=None) -> dict[str, int]:
    st = _settings(inc)
    new = store.unclustered_claims()
    if not new:
        print("claims: nothing new to cluster")
        return {"lexical": 0, "model": 0, "new_clusters": 0}
    existing = store.clusters()
    ex_tok = {c["id"]: tokens(c["text"]) for c in existing}
    ex_views = _cluster_views(store)
    stats = {"lexical": 0, "model": 0, "new_clusters": 0}
    assignments: dict[int, int] = {}

    # (a) lexical: join a canonical claim whose words nearly match
    pending: list[dict[str, Any]] = []
    for c in new:
        t = tokens(c["text"])
        best, best_id = 0.0, None
        for cid, et in ex_tok.items():
            j = jaccard(t, et)
            if j > best:
                best, best_id = j, cid
        if best_id is not None and best >= LEXICAL_ASSIGN:
            assignments[c["id"]] = best_id
            stats["lexical"] += 1
        else:
            c["_tok"] = t
            pending.append(c)
    if assignments:
        store.assign_clusters(assignments)
        assignments = {}

    # (b) group near-identical new claims so the model sees one representative each
    reps: list[dict[str, Any]] = []            # representative claims
    members: dict[int, list[int]] = {}         # rep id -> claim ids (incl. itself)
    for c in pending:
        placed = False
        for r in reps:
            if jaccard(c["_tok"], r["_tok"]) >= LEXICAL_GROUP:
                members[r["id"]].append(c["id"])
                placed = True
                break
        if not placed:
            reps.append(c)
            members[c["id"]] = [c["id"]]

    if not reps:
        return stats
    if not env("ANTHROPIC_API_KEY"):
        print("claims: ANTHROPIC_API_KEY not set — new claims stay unclustered", file=sys.stderr)
        return stats
    client = client or _client()
    at = _now()
    batch = 60
    for i in range(0, len(reps), batch):
        chunk = reps[i:i + batch]
        # existing canonical claims to show: the biggest, plus each new claim's closest neighbours
        show: set[int] = {cid for cid, _ in sorted(ex_views.items(), key=lambda kv: -kv[1])[:EXISTING_TOP]}
        for c in chunk:
            near = sorted(((jaccard(c["_tok"], et), cid) for cid, et in ex_tok.items() if jaccard(c["_tok"], et) >= 0.15), reverse=True)[:NEIGHBOURS]
            show.update(cid for _, cid in near)
        ex_by_id = {c["id"]: c for c in existing}
        ex_text = "\n".join(f"{cid} | {_clean(ex_by_id[cid]['text'], 200)}" for cid in sorted(show) if cid in ex_by_id) or "(none yet)"
        new_text = "\n".join(f"n{c['id']} | {c['text']}" for c in chunk)
        res = _ask(client, st["model"], CLUSTER_SYSTEM, CLUSTER_USER.format(subject=inc.subject, existing=ex_text, new=new_text), max_tokens=8000)
        assign = res.get("assign") if isinstance(res.get("assign"), dict) else {}
        groups = res.get("groups") if isinstance(res.get("groups"), dict) else {}
        made: dict[str, int] = {}
        for c in chunk:
            target = assign.get(f"n{c['id']}", assign.get(str(c["id"])))
            cid: int | None = None
            if isinstance(target, (int, str)) and str(target).isdigit() and int(target) in ex_by_id:
                cid = int(target)
            elif isinstance(target, str) and target in groups:
                if target not in made:
                    g = _valid_claim(groups[target]) or _valid_claim({**c, "entities": json.loads(c.get("entities") or "[]")})
                    if g is None:
                        continue
                    made[target] = store.add_cluster(g["text"], g["type"], g["checkable"], g["stance"], g["entities"], at)
                    existing.append({"id": made[target], "text": g["text"]})
                    ex_by_id[made[target]] = existing[-1]
                    ex_tok[made[target]] = tokens(g["text"])
                    stats["new_clusters"] += 1
                cid = made[target]
            else:
                # model skipped it or named a missing group: open a cluster from the claim itself
                g = _valid_claim({**c, "entities": json.loads(c.get("entities") or "[]")})
                if g is None:
                    continue
                cid = store.add_cluster(g["text"], g["type"], g["checkable"], g["stance"], g["entities"], at)
                existing.append({"id": cid, "text": g["text"]})
                ex_by_id[cid] = existing[-1]
                ex_tok[cid] = tokens(g["text"])
                stats["new_clusters"] += 1
            for claim_id in members[c["id"]]:
                assignments[claim_id] = cid
                stats["model"] += 1
        store.assign_clusters(assignments)
        assignments = {}
        print(f"  clustered {min(i + batch, len(reps))}/{len(reps)} new claims", flush=True)
    return stats


# ---- 3. stats + output --------------------------------------------------------------------------
def summarise(inc: Incident, store: Store) -> dict[str, Any]:
    lane_keys = {l["key"] for l in inc.lane_table()}
    lanes = {l["key"]: l for l in inc.lane_table()}
    posts = {p["id"]: p for p in store.all_posts()}
    claims = [c for c in store.claims_all() if c["cluster_id"] is not None and c["post_id"] in posts]
    by_cluster: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        by_cluster[int(c["cluster_id"])].append(c)
    try:
        end_ts = max(datetime.fromisoformat(p["created_at"]) for p in posts.values() if p["created_at"])
    except ValueError:
        end_ts = datetime.now(timezone.utc)
    out = []
    for cl in store.clusters():
        cid = int(cl["id"])
        members = by_cluster.get(cid) or []
        pids = sorted({c["post_id"] for c in members}, key=lambda pid: posts[pid]["created_at"] or "")
        if not pids:
            continue
        ps = [posts[pid] for pid in pids]
        views = sum(int(p["view_count"] or 0) for p in ps)
        lane_counts: dict[str, int] = defaultdict(int)
        for p in ps:
            if p["lane"] in lane_keys:
                lane_counts[p["lane"]] += 1
        first = ps[0]
        last24 = sum(int(p["view_count"] or 0) for p in ps if (end_ts - datetime.fromisoformat(p["created_at"])).total_seconds() <= 86400)
        series = [[p["created_at"], int(p["view_count"] or 0)] for p in ps]
        out.append({
            "id": cid, "text": cl["text"], "type": cl["type"], "checkable": bool(cl["checkable"]), "stance": cl["stance"],
            "entities": json.loads(cl["entities"] or "[]"),
            "verdict": cl["verdict"], "verdict_basis": cl["verdict_basis"],
            "verdict_sources": json.loads(cl["verdict_sources"] or "[]") if cl["verdict_sources"] else [],
            "posts": len(ps), "views": views, "accounts": len({p["author_handle"] for p in ps}),
            "copies": sum(1 for p in ps if p["copy_origin"]),
            "pushback_posts": lane_counts.get(PUSHBACK_KEY, 0),
            "lanes": [{"key": k, "label": lanes[k]["label"], "color": lanes[k]["color"], "posts": n}
                      for k, n in sorted(lane_counts.items(), key=lambda kv: -kv[1])],
            "first_seen": first["created_at"], "first_handle": first["author_handle"], "first_url": first["url"],
            "first_views": int(first["view_count"] or 0), "last_seen": ps[-1]["created_at"],
            "views_24h": last24, "series": series,
            "top_post": max(ps, key=lambda p: int(p["view_count"] or 0))["url"],
        })
    out.sort(key=lambda r: -r["views"])
    done = store.conn.execute("SELECT COUNT(*), COALESCE(SUM(n), 0) FROM claims_done").fetchone()
    return {
        "generated_at": _now(), "slug": inc.slug, "title": inc.title,
        "stats": {"posts_processed": int(done[0]), "claims": int(done[1]), "clusters": len(out),
                  "checkable": sum(1 for r in out if r["checkable"]),
                  "verified": sum(1 for r in out if r["verdict"]),
                  "unsupported_spreading": sum(1 for r in out if r["verdict"] in ("unsupported", "misleading") and r["views_24h"] > 0)},
        "clusters": out,
    }


def claims(inc: Incident, skip_model: bool = False) -> Path:
    store = Store(inc.db_path)
    if not skip_model:
        if env("ANTHROPIC_API_KEY"):
            client = _client()
            n = extract(inc, store, client)
            st = cluster(inc, store, client)
            print(f"claims: {n} posts extracted; clustered lexical={st['lexical']} model={st['model']} new canonical={st['new_clusters']}")
        else:
            print("claims: ANTHROPIC_API_KEY not set — summarising what is stored", file=sys.stderr)
    data = summarise(inc, store)
    store.close()
    (inc.out_dir / "claims.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    page = render_html(inc, data)
    s = data["stats"]
    print(f"wrote {page}  ({s['clusters']} canonical claims from {s['claims']} claims in {s['posts_processed']} posts; checkable={s['checkable']})")
    return page


# ---- page --------------------------------------------------------------------------------------
from .analysis import CSS, _fmt  # noqa: E402  (same look as the analysis page)

EXTRA_CSS = """
  .pill { display:inline-block; font-size:10px; letter-spacing:.04em; text-transform:uppercase; border-radius:999px; padding:1px 7px; margin-right:4px; border:1px solid var(--line); color:var(--ink-2); }
  .pill.fact { border-color:#5b8def66; color:#8fb4ff; } .pill.prediction { border-color:#c084fc66; color:#d8b4fe; } .pill.call_to_action { border-color:#2fbf8c66; color:#5fd9a8; }
  .pill.check { border-color:#e08a2e66; color:#f0b06a; } .pill.against { color:#ff8a80; } .pill.for { color:#7fd9a2; }
  .pill.unsupported, .pill.misleading { background:#5a1d1d; border-color:#ff5252; color:#ffb3b3; } .pill.supported { background:#163d2a; border-color:#2fbf8c; color:#9ff0c9; }
  .claim { font-size:13px; line-height:1.35; max-width:52ch; } .chips span { display:inline-block; font-size:10.5px; margin:2px 6px 0 0; }
  .chips i { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:4px; vertical-align:-1px; } svg.spark { display:block; }
  .filters { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; } .filters button { background:var(--surface); color:var(--ink-2); border:1px solid var(--line); border-radius:999px; padding:3px 10px; font-size:11.5px; cursor:pointer; }
  .filters button.on { color:var(--ink); border-color:var(--accent); } tr.hide { display:none; } details summary { cursor:pointer; color:var(--ink-3); font-size:11px; }
"""


def _spark(series: list[list[Any]], start: datetime, end: datetime, w: int = 120, h: int = 22) -> str:
    """Cumulative views over the window as a tiny SVG path."""
    if not series:
        return ""
    span = max(1.0, (end - start).total_seconds())
    total = sum(v for _, v in series) or 1
    pts, acc = [f"0,{h}"], 0
    for iso, v in series:
        x = (datetime.fromisoformat(iso) - start).total_seconds() / span * w
        pts.append(f"{x:.1f},{h - acc / total * (h - 2):.1f}")
        acc += v
        pts.append(f"{x:.1f},{h - acc / total * (h - 2):.1f}")
    pts.append(f"{w},{h - (h - 2)}")
    return f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline points="{" ".join(pts)}" fill="none" stroke="var(--accent)" stroke-width="1.5"/></svg>'


def render_html(inc: Incident, data: dict[str, Any], top_n: int = 200) -> Path:
    tz = ZoneInfo(inc.timezone)
    fmt = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%b %d, %I:%M %p").replace(" 0", " ")  # noqa: E731
    rows = data["clusters"][:top_n]
    start = datetime.fromisoformat(inc.start).replace(tzinfo=tz).astimezone(timezone.utc)
    end = datetime.fromisoformat(inc.end).replace(tzinfo=tz).astimezone(timezone.utc)
    try:
        end = max(end, max(datetime.fromisoformat(r["last_seen"]) for r in rows))
    except ValueError:
        pass
    s = data["stats"]
    trs = []
    for r in rows:
        pills = f'<span class="pill {r["type"]}">{html.escape((r["type"] or "").replace("_", " "))}</span>'
        if r["checkable"]:
            pills += '<span class="pill check">checkable</span>'
        if r["stance"] in ("for", "against"):
            pills += f'<span class="pill {r["stance"]}">{r["stance"]}</span>'
        if r["verdict"]:
            pills += f'<span class="pill {html.escape(r["verdict"])}" title="{html.escape(r.get("verdict_basis") or "")}">{html.escape(r["verdict"])}</span>'
        chips = "".join(f'<span><i style="background:{l["color"]}"></i>{html.escape(l["label"])} {l["posts"]}</span>' for l in r["lanes"][:3])
        verdict_html = ""
        if r["verdict"]:
            src = " ".join(f'<a href="{html.escape(u)}" style="color:var(--ink-2)">[{i + 1}]</a>' for i, u in enumerate(r["verdict_sources"][:4]))
            verdict_html = f'<details><summary>why</summary><div class="muted" style="max-width:52ch">{html.escape(r.get("verdict_basis") or "")} {src}</div></details>'
        trs.append(
            f'<tr data-type="{r["type"]}" data-check="{int(r["checkable"])}" data-stance="{r["stance"]}" data-verdict="{r["verdict"] or ""}">'
            f'<td><div class="claim">{html.escape(r["text"])}</div><div style="margin-top:4px">{pills}</div>{verdict_html}</td>'
            f'<td><a href="{html.escape(r["first_url"] or "#")}" style="color:var(--ink)">@{html.escape(r["first_handle"])}</a><div class="muted">{fmt(r["first_seen"])} · {_fmt(r["first_views"])} views</div></td>'
            f'<td class="n"><a href="{html.escape(r["top_post"] or "#")}" style="color:var(--ink)">{r["posts"]}</a></td><td class="n">{_fmt(r["views"])}</td>'
            f'<td class="n">{_fmt(r["views_24h"])}</td><td class="n">{r["accounts"]}</td><td class="n">{r["copies"]}</td>'
            f'<td><div class="chips">{chips}</div></td><td>{_spark(r["series"], start, end)}</td></tr>'
        )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(inc.title)} — claims</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<a class="back" href="./">← back to the chart</a>
<h1>{html.escape(inc.title)}: what is being claimed</h1>
<p class="sub">Every in-lane post with {_settings(inc)["min_views"]:,}+ views is read for the claims it makes; claims that assert the same thing
are grouped into one canonical claim. Views and posts count each post once per claim. "Last 24h" is views from posts in the final day of the window.
Checkable means a reporter could confirm or refute it from public records. Verdicts, where present, are advisory and link to their sources.</p>
<div class="tiles">
  <div class="tile"><div class="t">Canonical claims</div><div class="v">{s["clusters"]}</div><div class="d">from {s["claims"]:,} claims in {s["posts_processed"]:,} posts</div></div>
  <div class="tile"><div class="t">Checkable</div><div class="v">{s["checkable"]}</div><div class="d">factual claims a reporter could test</div></div>
  <div class="tile"><div class="t">Verified</div><div class="v">{s["verified"]}</div><div class="d">{s["unsupported_spreading"]} unsupported and still spreading</div></div>
</div>
<div class="filters" id="filters">
  <button data-f="all" class="on">All</button><button data-f="type:fact">Facts</button><button data-f="type:opinion">Opinions</button>
  <button data-f="type:prediction">Predictions</button><button data-f="type:call_to_action">Calls to action</button>
  <button data-f="check:1">Checkable</button><button data-f="stance:against">Against</button><button data-f="stance:for">For</button>
  <button data-f="verdict:unsupported">Unsupported</button>
</div>
<div class="wrap"><table id="t"><thead><tr><th>Claim</th><th>First made by</th><th class="n">Posts</th><th class="n">Views</th><th class="n">Last 24h</th><th class="n">Accounts</th><th class="n">Copies</th><th>Lanes</th><th>Spread</th></tr></thead>
<tbody>{"".join(trs)}</tbody></table></div>
<footer>Generated {html.escape(data["generated_at"][:16].replace("T", " "))} UTC · claims extracted and grouped by Claude · <a href="accounts.html" style="color:var(--ink-2)">who moved it</a> · <a href="analysis.html" style="color:var(--ink-2)">timing &amp; pushback</a></footer>
<script>
  const btns = document.querySelectorAll('#filters button'), rows = document.querySelectorAll('#t tbody tr');
  btns.forEach(b => b.addEventListener('click', () => {{
    btns.forEach(x => x.classList.toggle('on', x === b));
    const f = b.dataset.f; const [k, v] = f.split(':');
    rows.forEach(r => r.classList.toggle('hide', f !== 'all' && r.dataset[k] !== v));
  }}));
</script></body></html>"""
    path = inc.out_dir / "claims.html"
    path.write_text(page, encoding="utf-8")
    return path
