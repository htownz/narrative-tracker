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

## 5. Verification of checkable claims (`tracker/verify.py`) — built Oct 14

For canonical claims marked `checkable` with views above a threshold, Claude with web search
produces a verdict (`supported` / `unsupported` / `misleading` / `unverifiable`), a one-paragraph
basis, and sources. Stored with the claim; shown on the claims page and surfaced on the hub as
"Debunked but spreading" when an unsupported claim is still gaining views after its verdict.

Verdicts are advisory and sourced; the page shows the sources, not just the label.

## 6. Prediction (`tracker/features.py`, then a model) — feature table built Oct 14; model after 2-3 weeks of rows

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


---

# Phase 2 (planned Oct 15): correlation, learning, prediction

Where things stand after nine days of live operation plus the September backfill: 34K posts stored (11K drawn),
23K lane labels, 9.5K atomic claims in 6K canonical claims, 169 verdicts (95 supported, 52 misleading, 11
unsupported, 11 unverifiable), 5K feature rows with 491 already carrying a 24-hour outcome, 16 calendar events,
link-ring and control scores on every lane, a weekly brief. Everything below builds on that.

## What limits prediction today

1. **View counts are snapshots.** A post's views are what they were when it was pulled (the last two days are
   re-pulled; the top 500 refreshed weekly). "Views 24 hours later" is therefore reconstructed from when posts
   were created, not measured. Growth prediction needs real growth curves.
2. **Lanes are fixed hypotheses.** A narrative that was not in the lane set lives in `other` until an alert
   notices it. New-topic detection is the alerts clusterer, which is crude.
3. **One platform, no news signal.** Whether a claim crossed to mainstream coverage is inferred from news
   accounts posting on X. There is no direct measure of news coverage, and no second platform.
4. **Markets are one feed.** Kalshi only; no lane has correlated with price above |r| = 0.31 in nine days.
5. **No account memory.** Roles are recomputed per tracker from scratch; nothing records that an account has
   spread three claims later rated unsupported.
6. **No human labels yet.** Zero overrides; the few-shot loop is idle.

## Outcomes worth predicting (defined so they can be scored)

| # | Outcome | Label source | Horizon |
|---|---|---|---|
| O1 | Claim is in its tracker's top five by views gained | feature table (`gain_24h`, `top5_24h`) | 24h |
| O2 | Claim crosses into mainstream news (3+ outlets) | GDELT article counts for the claim's entities + key terms | 48h |
| O3 | Checkable claim will be rated unsupported or misleading | verifier verdicts | at first sight |
| O4 | A false claim collapses (views < 10% of peak) after an institutional denial | post-denial view series | 48h |
| O5 | Lane velocity leads a market or poll move | Kalshi + Polymarket candles, poll averages | 6–48h |

## Data to add, ranked by value per dollar

1. **View-count time series** — every run, re-look-up the 100 most-viewed posts of the last 48 hours per tracker
   and append (post, time, views) rows. About $1/day across ten trackers. Turns O1 and O4 into measured outcomes
   and gives "velocity at hour h" features.
2. **GDELT news mentions** (free) — hourly article counts per canonical claim (entities + two key terms) from the
   GDELT 2.0 DOC API. Gives O2, a news lead/lag, and "debunked in print but still spreading on X".
3. **Polymarket** (free) beside Kalshi for the same three races; poll averages as the slow outcome.
4. **Account registry** — a persistent table across trackers: first seen, follower history, roles per tracker,
   claims originated and carried, and the verdicts those claims got. Yields an account reliability score (share of
   carried claims later rated unsupported) and the "who touched it" predictor (once @spencerpratt and
   @nicksortor both carry a claim, what is its top-five probability?).
5. **Events from the data** — poll releases auto-added when a poll aggregator account posts first; later TLO
   bill actions and hearings for session mode.
6. **Bluesky firehose** (free) — origination vs. spread across platforms.

## Analytics and models, in sequence

1. **Insights page — built Oct 15 (`tracker/insights.py`, `/insights/`).** Descriptive correlations on existing
   data, auto-rebuilt each run: lane→lane lead/lag, event-type → lane response, originator → spread, verdict
   rates by lane, pushback lag vs. reach, control score vs. growth, news crossover, markets. Each with N and a
   plain-language reading written by rule. First readings (N=647 claim-days): a first poster with 1M+ followers
   doubles the chance of the top five (12% vs 6%); polls move lanes most (median 1.67×); 75 of 210 verified
   claims were unsupported or misleading; the lanes answered fastest are the biggest ones.
2. **Growth model (data ≥ 2 weeks, ~Nov 1).** Gradient-boosted classifier for O1 on the feature table with
   time-ordered validation; calibration shown on the hub; "Likely to grow" switches from the heuristic to model
   probability. AUC reported honestly, including when it is near 0.5.
3. **Mistruth-at-first-sight (verdicts ≥ 300).** Classifier for O3 from claim type, specificity, lane,
   originator reliability, account mix, source presence. Flags claims to verify first, which also cuts
   verification cost.
4. **Account reliability — built Oct 15 (`tracker/registry.py`).** Published per account on the amplifier map
   ("Who to believe, who to follow"); `first_reliability` / `first_early_lift` in the feature table. Scorecard
   (`tracker/scorecard.py`) and automatic poll events (`tracker/pollevents.py`) built the same day: first
   backtest has the top-three picks hitting 29% vs 7% base; poll events went from 16 hand-entered to ~50 found.
5. **Post-election retrospective.** Score every prediction made; publish what worked.
6. **Session mode (December).** One tracker per priority bill; outcome = bill action (referred, heard, voted,
   passed) from TLO; backfill the 2025 session from X search history and bill histories as training data. The
   real question for the firm: does narrative pressure on X predict a bill's movement, and with what lead?

## Cost

twitterapi.io: about +$1/day for the view series. GDELT, Polymarket, Bluesky: free. Anthropic: unchanged
(the models run on the runner with scikit-learn). GitHub Actions: free.
