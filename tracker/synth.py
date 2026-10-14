"""Load a clearly-labelled SYNTHETIC sample into the store so the chart can be built and reviewed
without an API key. Every handle is sample_acct_NNN; nothing here is a real post.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import PUSHBACK_KEY, Incident
from .store import Store

SAMPLE_LANES = [
    {"key": "skipped_rally", "label": "He skipped the Trump rally",
     "description": "Abbott is avoiding Trump's San Antonio rally and that shows distance from the president.",
     "keywords": ["rally", "san antonio", "skip", "scheduling conflict"]},
    {"key": "polls_tightening", "label": "The race is tied",
     "description": "New polls show Abbott and Hinojosa within a point and the governor is in trouble.",
     "keywords": ["poll", "tied", "48", "47", "toss-up"]},
    {"key": "property_tax", "label": "$3,000 property tax promise",
     "description": "Abbott's plan to eliminate school property taxes and save homeowners $3,000 a year.",
     "keywords": ["property tax", "$3,000", "appraisal", "school taxes"]},
    {"key": "soft_on_crime", "label": "Hinojosa is hostile to police",
     "description": "Abbott's attack line that Hinojosa is the most anti-law-enforcement legislator in Austin.",
     "keywords": ["law enforcement", "police", "crime", "hostile"]},
    {"key": "data_centers", "label": "Data center freeze was a stunt",
     "description": "Claims the data center moratorium is election-year cover after years of subsidies.",
     "keywords": ["data center", "moratorium", "freeze", "subsid"]},
]

# One seed sentence per lane plus 'copy' seeds that get repeated near-verbatim by many accounts.
SEEDS = {
    "skipped_rally": [
        "Abbott says a scheduling conflict keeps him from the San Antonio rally. Sure.",
        "Governor who ran as Trump's best friend now can't find a day for his rally. Noted.",
        "Interesting that the Abbott campaign won't say what the conflict is.",
    ],
    "polls_tightening": [
        "48-47. In Texas. For governor. Let that sink in.",
        "Second poll in a row with the governor's race inside the margin of error.",
        "Abbott's internal numbers must look different from the public ones, because he keeps saying he's not worried.",
    ],
    "property_tax": [
        "Abbott says his plan cuts the average homeowner's property tax bill by $3,000 a year.",
        "Eliminating school property taxes would be the biggest tax cut in state history.",
        "Where does the $3,000 number come from? Show the math.",
    ],
    "soft_on_crime": [
        "Abbott in Arlington: no legislator in Austin is more hostile to law enforcement than Hinojosa.",
        "The crime attack is the whole closing argument. Expect it in every ad for three weeks.",
        "Abbott finally said her name. That tells you what the internal polling says.",
    ],
    "data_centers": [
        "He took the donations, gave the tax breaks, then froze the projects three months before the election.",
        "The data center moratorium is an audit, not a ban. Read the order.",
        "Rural Texas asked for this for two years and got it the summer before a close race.",
    ],
    PUSHBACK_KEY: [
        "None of this is new. Every one of these 'scandals' was in the papers in August.",
        "A one-point poll is not a story, it is a Tuesday in a midterm year.",
        "The rally thing is a scheduling conflict. Governors miss events every week.",
        "Can we talk about policy instead of who went to which rally?",
    ],
}

COPY_SEEDS = {
    "skipped_rally": "Governor Abbott will not appear with President Trump in San Antonio. His campaign cites a scheduling conflict. One month before the election.",
    "polls_tightening": "NEW: Texas governor poll. Abbott 48, Hinojosa 47. Undecided 3. Likely voters.",
    "soft_on_crime": "Abbott: I don't know of any legislator in Austin who is more hostile to law enforcement than Gina Hinojosa.",
    PUSHBACK_KEY: "Reminder that the same accounts said the Senate race was over in June.",
}


def _views(rng: random.Random, big: float = 0.03) -> int:
    v = int(rng.lognormvariate(8.2, 0.9))  # median ~3.6K
    if rng.random() < big:
        v = int(rng.uniform(300_000, 2_400_000))
    return max(1000, v)


def load(inc: Incident, n_total: int = 720, seed: int = 7) -> None:
    rng = random.Random(seed)
    tz = ZoneInfo(inc.timezone)
    t0 = datetime.fromisoformat(inc.start).replace(tzinfo=tz) + timedelta(hours=9)
    hours = 7 * 24

    # lane start hours and weights (pushback grows over time)
    plan = {
        "skipped_rally": (10, 0.22, 0.9),
        "polls_tightening": (30, 0.24, 0.8),
        "property_tax": (0, 0.14, 0.4),
        "soft_on_crime": (70, 0.16, 1.1),
        "data_centers": (20, 0.08, 0.5),
        PUSHBACK_KEY: (14, 0.16, 1.4),
    }
    lanes = list(plan.keys())
    weights = [plan[l][1] for l in lanes]

    inc.lanes = SAMPLE_LANES
    store = Store(inc.db_path)
    store.conn.execute("DELETE FROM posts")
    rows = []
    by_lane: dict[str, list[dict]] = {l: [] for l in lanes}
    acct = 0

    def mk(lane: str, when: datetime, text: str, views: int, parent: dict | None, kind: str | None, copy_of: str | None):
        nonlocal acct
        acct += 1
        pid = f"9{acct:012d}"
        handle = f"sample_acct_{acct:03d}"
        row = {
            "id": pid, "created_at": when.astimezone(timezone.utc).isoformat(), "view_count": views,
            "like_count": int(views * rng.uniform(0.005, 0.04)), "retweet_count": int(views * rng.uniform(0.001, 0.01)),
            "reply_count": int(views * rng.uniform(0.0005, 0.004)), "quote_count": int(views * rng.uniform(0.0002, 0.002)),
            "author_handle": handle, "author_name": f"Sample Account {acct}", "author_avatar": "",
            "author_followers": int(rng.lognormvariate(8.5, 1.4)), "text": text,
            "url": f"https://example.invalid/{handle}/status/{pid}",
            "in_reply_to_id": parent["id"] if parent and kind == "reply" else None,
            "quoted_id": parent["id"] if parent and kind == "quote" else None,
            "retweeted_id": None, "in_dataset": 1, "raw": json.dumps({"synthetic": True}),
        }
        row["_lane"] = lane
        row["_copy"] = copy_of
        rows.append(row)
        by_lane[lane].append(row)
        return row

    # organic posts
    for _ in range(n_total):
        lane = rng.choices(lanes, weights)[0]
        start, _, growth = plan[lane]
        # time: after lane start, skewed by growth (higher growth = later mass)
        u = rng.random() ** (1 / growth)
        when = t0 + timedelta(hours=start + u * (hours - start - 6) + rng.uniform(0, 1.5))
        opener = rng.choice(["", "", "So.", "Well well.", "Reading this twice.", "From Austin:", "Not surprised.",
                             "Saw this earlier:", "For the record,", "Hmm.", "Quick note.", "Here we go.",
                             "This is the story today.", "Keep an eye on this."])
        middle = rng.choice(["", "", "", "I keep coming back to it.", "Nobody in Austin is surprised.",
                             "The timing is the whole story.", "Three weeks to go.", "Houston is watching."])
        closer = rng.choice(["", "", "Thread.", "Source in replies.", "Texas politics, folks.", "Big if true.",
                             "Watch this space.", "More to come.", "Somebody ask him.", "Bookmark it.",
                             "Vote.", "Tell your neighbors.", "Not a drill.", "Just saying."])
        text = f"{opener} {rng.choice(SEEDS[lane])} {middle} {closer}".strip().replace("  ", " ")
        parent, kind = None, None
        earlier = [p for p in by_lane[lane] if p["created_at"] < when.astimezone(timezone.utc).isoformat()]
        if lane == PUSHBACK_KEY and rng.random() < 0.55:
            pool = [p for l in lanes if l != PUSHBACK_KEY for p in by_lane[l]
                    if p["created_at"] < when.astimezone(timezone.utc).isoformat()]
            if pool:
                parent, kind = rng.choice(pool), "quote"
        elif earlier and rng.random() < 0.6:
            parent = rng.choices(earlier, [max(1, p["view_count"]) ** 0.5 for p in earlier])[0]
            kind = "quote" if rng.random() < 0.65 else "reply"
        mk(lane, when, text, _views(rng), parent, kind, None)

    # copy-paste clusters: same words posted again by 4-14 accounts over the following hours
    for lane, seed_text in COPY_SEEDS.items():
        start, _, _ = plan[lane]
        when0 = t0 + timedelta(hours=start + rng.uniform(2, 20))
        origin = mk(lane, when0, seed_text, int(rng.uniform(400_000, 1_900_000)), None, None, None)
        for i in range(rng.randint(4, 14)):
            when = when0 + timedelta(hours=rng.uniform(0.3, 48))
            variant = seed_text if rng.random() < 0.7 else seed_text.replace("Abbott", "Gov. Abbott")
            mk(lane, when, variant, _views(rng, big=0.08), None, None, origin["id"])

    store.upsert_posts([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    store.set_lanes({r["id"]: r["_lane"] for r in rows}, "synthetic")
    store.set_meta("synthetic", True)
    store.set_meta("pulled_at", None)
    print(f"loaded {len(rows)} SYNTHETIC posts across {len(lanes)} lanes into {inc.db_path}")
    store.close()
