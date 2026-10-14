# Roadmap: from tracking to predicting

What exists today (Oct 14, 2026): nine trackers pulling X four times a day, Claude sorting every post
into fixed narrative lanes, same-words copy detection, an amplifier map (who originates, who copies,
who pushes back), early-warning alerts for new clusters and lane surges, Kalshi price overlays with
lead/lag, pushback-effectiveness analysis, and a review page for correcting labels. All of it is
descriptive: it shows what happened.

The next phase makes it *predictive* and *claim-aware*: what is being asserted, which assertions are
false, who is pushing them in a coordinated way, which legislation or campaign event they attach to,
and which of the stories forming right now will be big tomorrow.

## 1. Claims layer (`tracker/claims.py`) — built Oct 14

Lanes answer "what is this post arguing?". Claims answer "what does this post assert is true?".

- Claude extracts atomic claims from every in-lane post: short canonical statement, type
  (`fact` / `opinion` / `prediction` / `call_to_action`), `checkable` yes/no, stance (for/against the
  subject), entities named (people, bills, agencies, places).
- Stored in a `claims` table in the incident's sqlite (post_id, claim_id, text, type, checkable,
  stance, entities, extracted_at). Extraction is incremental: only posts without claims are sent.
- Claims are clustered into canonical claims: lexical candidates (shared entities + word overlap),
  then Claude merges near-duplicates into one canonical statement. Each canonical claim carries
  first-seen, post count, total views, distinct accounts, lanes it appears in, and a spread curve.
- Output: `claims.json` and `claims.html` per tracker (a table of canonical claims sorted by views,
  with the first post that made each one, its spread, and whether it is checkable), linked from the
  chart footer and the hub.

Why first: everything below (mistruths, narrative-control, prediction) keys off a claim, not a post.

## 2. Narrative-control score (`tracker/control.py`) — built Oct 14

A 0–100 score per lane and per canonical claim for "how much does this look like a push rather than
organic spread", from features we already have:

- copy ratio: share of posts that are same-words copies of an earlier post
- concentration: share of lane views coming from the top five accounts
- new-account share: posters whose account is under 90 days old (author `createdAt` is in the raw)
- burstiness: share of posts landing inside the single busiest hour
- copy timing regularity: how evenly spaced the copies are (bots and paid rings are regular)
- originator-to-copier ratio and whether the originator was a candidate/campaign account

Shown on the analysis page as a table with the features exposed (never a bare number), and on the
hub as "Coordinated pushes" when a lane or claim crosses 70.

## 3. Human corrections as few-shot examples — built Oct 14

`incidents/<slug>.overrides.csv` already corrects labels after the fact. The same rows become
examples inside the classification prompt (up to 20, most recent first), so a correction teaches the
classifier instead of only patching one post. Same mechanism later for claim extraction.

## 4. Events calendar and correlation (`incidents/events.yaml`, `tracker/events.py`) — built Oct 14

A shared, hand-maintained list of things that happen off X: bill filings, hearings, floor votes,
court rulings, debates, ad buys, rallies, poll releases, endorsements. Each has a time, label, type
and the trackers it bears on.

- Chart: event markers on the timeline.
- Analysis page: event → lane response table (views in the 12 hours after vs. the 12 before, per
  lane), so "the Senate hearing on data centers moved the `abbott_failing_texans` lane 4×" is a
  line in a table rather than a hunch.
- Claims page: claims that first appeared within 6 hours of an event are tagged with it.

Later: pull the calendar automatically from Texas Legislature Online and the campaigns' public
schedules instead of by hand.

## 5. Verification of checkable claims

For canonical claims marked `checkable` with views above a threshold, Claude with web search
produces a verdict (`supported` / `unsupported` / `misleading` / `unverifiable`), a one-paragraph
basis, and sources. Stored with the claim; shown on the claims page and surfaced on the hub as
"Debunked but spreading" when an unsupported claim is still gaining views after its verdict.

Verdicts are advisory and sourced; the page shows the sources, not just the label.

## 6. Prediction (`tracker/features.py`, then a model)

Every run appends one row per canonical claim (and per lane) to `data/features/<slug>.csv`:
age in hours, posts and views so far, growth over the last 1h/6h/24h, distinct accounts,
originator reach, copy ratio, control score, whether it hit an event window, Kalshi price and
24h change. Labels are filled in retroactively: views 24h and 72h later, whether it reached the
top five claims, whether it crossed into mainstream (a post from a news account).

After two to three weeks there is enough to fit a small gradient-boosted model per outcome
("will this claim be in the top five tomorrow?"), scored on each run, with the top predictions
listed on the hub as "Likely to grow". Until then, the ranked heuristics (growth × control score ×
originator reach) stand in.

## Also on the list

- Second source: Bluesky's public firehose for the same queries (cheap, no credits).
- Coordination detection beyond same-words: accounts that post within the same minute across
  trackers, or that always quote the same originator.
- Session mode: an operator chat over a tracker's data ("what changed since this morning?").
- Pull the Talarico calibration to its full six days and compare lane counts to the original chart.
- Widen the thin issue trackers (vouchers, grid) with more queries.
