# Narrative Tracker

Charts how a political story spreads on X: every post with 1K+ views on a subject over a window,
sorted into the narratives it pushes, with quote/reply links, near-verbatim repeats, and a time scrubber.
Modelled on the "Talarico incident, five days of posts" chart.

```
pull  ->  discover lanes (Claude)  ->  classify  ->  dedup  ->  export data.json  ->  render index.html
```

## Setup

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # add TWITTERAPI_IO_KEY and ANTHROPIC_API_KEY
```

- `TWITTERAPI_IO_KEY` — https://twitterapi.io/dashboard. Pay-per-result: 15 credits ($0.00015) per tweet returned,
  15-credit minimum per call. `python run.py pull <yaml> --dry-run` prints the calls before spending anything.
  Free tier (no recharge balance) is one request every 5 seconds; the client detects that and paces itself.
  The Abbott pilot (40 queries, ~100 calls, 727 posts) cost about $0.17 and took ~12 minutes at that pace.
  Pull progress is saved per query, so a killed run resumes with the same command; `--budget 160` stops cleanly
  after N seconds for shells with a time cap.
- `ANTHROPIC_API_KEY` — used for lane discovery and classification. Without it, classification falls back to the
  keyword lists in the YAML.

## Run the Abbott pilot

```powershell
python run.py all incidents/abbott-oct-2026.yaml
```

That pulls seven days of posts, has Claude propose 4–6 lanes (written back into the YAML — edit them and re-run
`classify --relabel` if you disagree), labels every post, finds same-words clusters, and writes
`out/abbott-oct-2026/index.html` — one self-contained page you can open, email, or drop on Netlify.

Step by step, when you want control:

```powershell
python run.py pull      incidents/abbott-oct-2026.yaml          # ~40 search calls for 5 queries x 8 days
python run.py discover  incidents/abbott-oct-2026.yaml --accept # proposes lanes, saves them to the YAML
python run.py classify  incidents/abbott-oct-2026.yaml
python run.py dedup     incidents/abbott-oct-2026.yaml
python run.py export    incidents/abbott-oct-2026.yaml
python run.py render    incidents/abbott-oct-2026.yaml
```

## New incident

Copy `incidents/abbott-oct-2026.yaml`, change `slug`, `title`, `subject`, `window`, and `queries`.
Leave `lanes: []` to discover them, or write them by hand (`key`, `label`, `description`, optional `keywords`).

## Demo without keys

```powershell
python run.py synth incidents/abbott-oct-2026.yaml
```

Loads a clearly-labelled synthetic sample (every handle is `sample_acct_NNN`) so the chart can be reviewed.
Delete `data/<slug>.sqlite` before running a real `pull` for the same incident.

## What the chart encodes

- Lane rows = narratives; `pushback` is always the last lane. `other` posts are stored but not drawn (`export --include-other` to draw them).
- Dot x = time, dot size = views, dot height within the lane = views (log scale).
- White dot = 90%+ the same words as an earlier post (word containment ≥ 0.90 of the shorter post, 3-word shingles to find candidates); white line back to the origin; "same words ×N" pill on origins with 3+ repeats, "from N accounts" or "all from 1 account".
- A post that repeats a drawn post is drawn whatever its own views (it sits at the floor of the lane and inherits the ring's lane); a ring of small posts only is not drawn.
- Coloured curve = this post quoted or replied to that one (colour of the quoting post's lane).
- Counters: posts pushing the story (all non-pushback lanes), repeated posts, posts pushing back — all as of the scrub time.
- Avatars appear on the 40 most-viewed posts; `render` fetches them and embeds them as data URIs so the page
  has no external image dependencies (hosted viewers such as claude.ai artifacts block outside images).
- Quoted / replied-to posts inside the window at 1K+ views are also labelled and drawn (slightly faded) so quote
  lines have something to land on.

## Layout

```
incidents/     one YAML per narrative
tracker/       pull.py (twitterapi.io), lanes.py (Claude), dedup.py, graph.py, export.py, render.py, synth.py
web/           index.html — the D3 chart; /*__DATA__*/ gets replaced with the data on render
data/          sqlite per incident (gitignored)
out/<slug>/    data.json + index.html (gitignored)
```

## Pilot result (Oct 6–13, 2026)

Claude proposed six lanes from the top 150 posts: Hinojosa surging / Abbott slipping (polls, DGA money),
Abbott hiding / dodging debates, Flags for Israel not Uvalde, Abbott serves donors / fails Texans,
Abbott strong / Hinojosa too left, Abbott vs Sharia courts. 365 posts drawn, 80 links, 41 pushback.
Only 4 near-verbatim repeats — this week's Abbott discourse was quote-driven, not copy-paste-driven.

## Live trackers and the scheduled job

`incidents/*-live.yaml` use `window.end: today` (the window grows each run; the last `refresh_days` days are
re-pulled for new posts and fresher view counts). `.github/workflows/daily.yml` runs every six hours:
pull → classify new posts → dedup → export → render → accounts → analysis → alerts, for every live incident
(candidates first, then `issue-*`), commits `data/` and `out/`, and publishes `site/` to GitHub Pages
(hub at the root, each tracker under `/<slug>/`, the cross-tracker amplifier map under `/accounts/`, the
insights page under `/insights/`).
Lanes are discovered on the first run and then frozen so days stay comparable; `python run.py report`
shows what is landing outside them.

Repo secrets needed: `TWITTERAPI_IO_KEY`, `ANTHROPIC_API_KEY`. Optional: `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` to receive alerts. Trigger a run by hand from the Actions tab, or commit a change to `.run`
(one incident path per line, or the word `live` for all live trackers).

Each incident gets `PULL_BUDGET` seconds of pulling per run (default 13 min); a pull that hits the budget resumes on
the next run and the chart notes "pull still in progress". Live incidents should be pulled only by the job — the
SQLite stores are committed, and a local run pushed from your PC would conflict with the job's commits. Run one-off
incidents (fixed window) wherever you like.

## Where the data lives

The per-tracker SQLite stores (`data/<slug>.sqlite`) live in an S3-compatible bucket, not in git: committing
100 MB of binaries four times a day bloated the repo and one store crossed GitHub's 100 MB file limit. The job
runs `python -m tracker.bucket pull` after checkout and `push` before it commits, writes a dated snapshot every
Sunday (`snapshots/<date>/`, eight kept), and falls back to committing the stores to git if the bucket is
unreachable so no run's work is dropped. Git keeps the text outputs (`out/`, `data/features/*.csv`, the YAMLs).

Any S3-compatible provider works (Cloudflare R2's free tier, Railway Buckets, AWS S3, Backblaze B2) through
five GitHub secrets / `.env` variables: `BUCKET_ENDPOINT`, `BUCKET_NAME`, `BUCKET_ACCESS_KEY`,
`BUCKET_SECRET_KEY`, `BUCKET_REGION` (default `auto`). Without them the job behaves as before (stores in git).
On a laptop, `python -m tracker.bucket pull` fetches the current stores; `list` shows what the bucket holds.

## Backfilling and long windows

Move a live tracker's `window.start` earlier and the next runs pull the missing days (resumable; nothing already
stored is re-pulled). For a big backfill put `budget=3600` on its own line in `.run` next to the incident paths
so the run pulls for up to an hour per incident instead of the usual 13 minutes; classification and claims
catch up over the following runs. Charts whose window is longer than three weeks open on the last 14 days,
with a "Showing: last 7 / 14 / 30 days / the whole window" toggle under the subtitle (`?days=N` or `?days=all`).

## Site navigation

`python -m tracker.site site` assembles the published site: every tracker's pages are copied into
`site/<slug>/` with one navigation bar on top — a tracker switcher that keeps you on the same tab (claims to
claims, timing to timing), tabs for the tracker's pages, the hub and the amplifier map — and the hub is built
with its cards grouped into races and people, issues and archive, each card linking straight to the
tracker's pages. The bar hides itself in video mode (`?video=1`).

## Pages each tracker produces

| File | What it is |
|---|---|
| `index.html` | the chart — lanes, links, scrubber, Kalshi price strip when `markets:` is set |
| `accounts.html` | who moved it: roles (originator / bridge / copier / pushback), reach, lane mix, first movers |
| `analysis.html` | lane timing, half-life, pushback lag and share, lane-velocity vs market price lead/lag |
| `review.html` | lane spot-check: 20 posts per lane with a dropdown; builds the lines for `incidents/<slug>.overrides.csv` |
| `analysis.html` (events section) | how each lane moved in the 12h after each calendar event |
| `analysis.html` (narrative control section) | per-lane and per-claim control score with its five features |
| `claims.html` | what is being claimed: atomic claims extracted from every in-lane post, grouped into canonical claims with type, checkable flag, stance, first poster, spread, verdict |
| `alerts.json` | early warning: stories forming outside the lanes, lanes surging (also in the run summary / Telegram) |
| `<slug>.mp4` | `python run.py video` — 60-second play-through, 1920x1296 |

## Correcting lanes

Open `<slug>/review.html`, change the dropdown on any mislabelled post, copy the box, paste into
`incidents/<slug>.overrides.csv` (`post_id,lane`, one per line) and commit. `classify` applies the file last on
every run, so corrections survive re-labelling and live refreshes. The newest 20 corrections also go into the
classification prompt as labelled examples, so each one teaches the classifier about the posts that follow,
not only the post it fixed. If a whole lane is wrong, edit its label or description in the YAML and run
`classify --relabel`.

## View counts

Posts keep the view count from when they were first pulled (the original chart's convention), except the last
`refresh_days` days, which are re-pulled each run, and the 100 most-viewed posts between two and seven days old,
which every live run re-looks-up (two calls). Every sighting of a post at 1K+ views is appended to `view_history`,
so growth is measured: the feature table's `gain_24h` / `gain_72h` come from the history when it covers a claim
(`measured=1`) and from when posts appeared otherwise. Once a week (the Sunday 6:17 AM CT run) `refresh-views`
re-looks-up each tracker's 500 most-viewed posts (10 calls, about 8 cents) so the big numbers stay current.

## Tests

`pip install pytest && pytest -q` — dedup rule, window and DST math, tweet flattening, store upserts, lane save,
overrides. `.github/workflows/test.yml` runs them on Ubuntu and Windows, Python 3.10 and 3.12, plus a synthetic
end-to-end render, on every push that touches code.

## Run health

The hub shows the twitterapi.io balance (red under $1), the last run time, and any live tracker with no lanes,
unlabelled posts, a partial pull or query errors. `site/status.json` carries the same data for scripts.

## What Claude costs

Every model call the tracker makes (classify, discover, extract, cluster, alerts, brief, verify) logs its
tokens and web searches to `data/usage.csv` (`tracker/usage.py`; committed by the job). `python -m
tracker.usage` prints the totals by day and by step; `tracker.status` puts them in `status.json` and the hub's
health strip shows "Claude ~$ this run · today · 7d". The dollar figures are estimates at the list prices in
`usage.PRICES` (Sonnet 5.5: $3 in / $15 out per million tokens; web search $10 per 1,000) — the Anthropic
console is the bill. Verification is the expensive step per call (each is one Sonnet call with up to five web
searches and their results as input); clustering is the expensive step per run (every batch re-reads the 150
biggest canonical claims plus neighbours).

## Markets

```yaml
markets:
  - label: "Abbott wins — Kalshi"
    series: GOVPARTYTX
    ticker: GOVPARTYTX-26-R
```
Kalshi's election API is public. Tickers in use: `GOVPARTYTX-26-R/D` (governor), `SENATETX-26-R/D` (Senate),
`KXLTGOVTX-26-GOP/DEM` (lieutenant governor).

## Alerts

```yaml
alerts: { window_hours: 24, min_posts: 8, min_views: 100000, surge_ratio: 3.0 }
```
A new-story alert includes a ready-to-paste lane block; add it to the YAML and run `classify --relabel`.

## Claims

Lanes say what a post argues; claims say what it asserts is true. `python run.py claims incidents/x.yaml`
(also part of `all`) sends every in-lane post with `claims.min_views`+ views to Claude once, gets back
0–3 atomic claims (text, `fact`/`opinion`/`prediction`/`call_to_action`, checkable, stance, entities),
and groups them into canonical claims: a new claim joins an existing canonical claim when the words
nearly match, otherwise Claude decides whether it is the same assertion or a new one. Everything is
incremental and lives in the `claims`, `claims_done` and `claim_clusters` tables of the tracker's sqlite.

```yaml
claims: { min_views: 2000, per_run: 300, batch_size: 25 }   # all optional; per_run bounds the model cost per run
```
A backlog clears over a few runs (300 posts per run per tracker, biggest first). `claims.json` carries
every canonical claim with its spread series; the verdict columns are filled by the verification step
(see ROADMAP.md).

## Verification

`python run.py verify incidents/x.yaml` (part of `all`, right after `claims`) takes the biggest canonical
claims that are checkable facts without a verdict (`verify.min_views`, default 100K views; `verify.per_run`,
default 5) and has Claude verify each with web search against primary records and established reporting:
`supported`, `unsupported`, `misleading` or `unverifiable`, with a short basis and the sources. Verdicts show
on `claims.html` (filter "Unsupported"; "why" opens the basis and sources) and the hub lists unsupported or
misleading claims as "Debunked but spreading", the ones still gaining views first. Verdicts are advisory —
the sources are the point. Each verification is one model call with up to `verify.max_searches` searches,
so the per-run cap is what bounds the cost.

```yaml
verify: { min_views: 100000, per_run: 5, max_searches: 5 }   # all optional
```

## Mistruth at first sight

`python run.py mistruth` (the job runs it before the per-tracker loop and again at assembly) trains a classifier
on every verified canonical claim across trackers — label 1 for `unsupported` or `misleading`, 0 for
`supported`; `unverifiable` is left out — from only what is visible when the claim first appears: its wording
(numbers, percentages, money, years, quotes, attribution words, absolutes, negation, length, entity count), its
stance, the first poster (log followers, whether an official from `incidents/officials.yaml`, which platform,
first-post views), whether it followed a calendar event, the hour, and three out-of-time priors — the first
poster's, the lane's and the tracker's share of false claims among claims verified *before* this one appeared,
so no row sees its own verdict. Validation is time-ordered (train on the earliest 70%, test on the latest 30%);
logistic regression and gradient boosting are both tried and the better held-out AUC is kept, then refit on
everything. The report (`out/mistruth/model.json`) carries AUC, Brier, the precision of the top-scored fifth,
a five-bin calibration table and the top features, and its reading says plainly when the model is not beating
the base rate. Needs 150 verified claims; below that it writes a status line and nothing is flagged.

What the scores do: every unverified checkable claim gets a score (`out/<slug>/mistruth.json`); the top fifth
across trackers is "verify first". On each tracker's `claims.html` those carry a dashed **verify first** pill
and a filter; the hub lists the biggest under "Claims to verify first"; the insights page shows the model
report (section 11); and `verify` takes half its slots by views as before and the other half from the
highest-scored claims above `min_views`, so verification spends on what looks most likely to be false as well
as on what is spreading most. Scores are a queue for checking, never a verdict: nothing on the site calls an
unverified claim false. One caveat the page states: the training set is the claims verification chose — the
biggest ones — so scores on smaller claims are extrapolation. First fit (Oct 19, 536 verdicts): AUC 0.69, the
top fifth 66% false against a 42% base rate; stance against the subject, entity count and absolutes carry it.

## Weekly brief

`python run.py brief` (the job runs it on the Monday 6:17 AM CT run, or any run whose `.run` has a line saying
`brief`) computes one week of facts per live tracker — views and posts against the prior week, lanes gaining
or fading, the week's biggest canonical claims and their verdicts, claims that failed verification, who
originated and who carried, link rings and coordinated pushes, the market move, the calendar events and the
lanes that answered them — and has Claude write a plain-language brief from those facts and nothing else:
headline, a summary that doubles as the Telegram message, a section per race (issues grouped), and what to
watch. Published at `brief/` on the site with the numbers behind it in a fold-out, featured on the hub, archived
by date; sent to Telegram when `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are set.

## Elected rhetoric

`incidents/officials.yaml` lists elected officials, candidates, agencies and public bodies by X handle (add
freely; a wrong handle never matches). Their posts are surfaced wherever a tracker is read: "What electeds
said" on each analysis page (14 days, newest first, with the lane each post landed in), a line per tracker on
the client desk, the hub's "What electeds said this week" by reach, and the weekly brief, which is told to say
what office-holders said and into which lane.

## Message-doc phrases

An incident YAML may list `phrases:` — the anchor lines a coordinated campaign hands its carriers. `python run.py
phrases` (part of `all`) counts each phrase over the last 14 days in every post in the tracker's store (all
platforms) and every article in the press store: posts, accounts, views, articles, outlets, who was first (X or
the press), the top accounts, the lanes, and a day-by-day. Shown on the analysis page ("Message-doc phrases")
and the client desk. The HISD tracker carries the pro-reform campaign's lines ("largest turnaround in Texas
history", "no student attends a failing school", "regardless of zip code", the 93-to-197 figure); the sweep's
outlets (Texas 2036, Good Reason Houston, GHP, The 74, RealClearEducation, City Journal) are in the feed list.

## Client desk

`incidents/clients.yaml` maps each client and prospect to the policy areas it cares about, the trackers that
watch them, the legislation to follow and the elected voices that matter (nothing confidential: the repo is
public). `python run.py clients` (the job runs it at assemble time) renders `/clients/`: per client and area,
the trackers' last seven days — views, top lanes, biggest claims with verdicts, what is gaining, flagged
pushes — beside the legislative hooks and a "to confirm" line wherever a mapping is a guess. The long-term
issue trackers that serve it (`issue-*-live`, 26 of them: hemp and THC, housing, workforce, autonomous vehicles,
school governance, maternal health, water and flood, transportation, Houston-region local government, election
integrity, CenterPoint, local control, the border, broadband, economic development, victims' services,
financial services, K-12, tort reform, oil and gas, food security, plus the original data centers, grid,
property tax, vouchers and 765-kV) start Oct 1, 2026 and run through the 2027 session. The job pulls the race
trackers every run and the issue trackers on the 05:17 and 17:17 UTC runs; the race trackers end Nov 3.

## Platforms beyond X

Every tracker's store holds posts from more than one platform (`posts.source`: `x`, `bsky`, `yt`, `truth`);
the chart still plots X alone, because it plots views and the others' counts are not comparable, while
classification, same-words detection, claims, control scores and the account tables see every platform.
The claims page shows which platforms carried each canonical claim, which had it first and the lag; the
insights page's "Where claims start" block sums that up.

- **Bluesky** (`tracker/bsky.py`, part of `all`): the tracker's queries, X operators stripped, against the
  official search API, day by day and resumable. Bluesky has no view counts, so a post's `engagement` (likes +
  reposts + replies + quotes) is its threshold, 25 by default. The public endpoint refuses datacenter
  addresses, so the job signs in with `BSKY_HANDLE` / `BSKY_APP_PASSWORD` (a free account's *app password*,
  never the account password); without them the step logs why it skipped. `bluesky: false` in an incident
  YAML turns it off.
- **YouTube** (`tracker/youtube.py`): once a day per tracker when `YOUTUBE_API_KEY` is set (Google Cloud
  console, YouTube Data API v3, free 10,000-unit quota; a search is 100 units). Videos are posts with real
  view counts; the title and description are the text; the channel is the account (`yt:<channel id>`).
- **Truth Social** (`tracker/truth.py`): Trump's posts from the trumpstruth.org public archive feed. A post
  that names a tracker's subject becomes a calendar event (type `trump`) and a post in that tracker's store,
  always above threshold, so "Trump said it on Truth Social, X screenshotted it" shows up as Truth Social
  first with the lag. No other Truth Social account is read; there is no official API.
- **Not read, by decision:** TikTok and Facebook/Instagram/Threads, whose research APIs are closed to
  commercial users (an academic partner can open both); Ground News, which has no API (its outlet-lean idea
  is replicated in `incidents/outlets.yaml`); third-party scrapers of any platform.

## The press

`python -m tracker.newsfeeds` (the job runs it before each pull) reads the RSS feeds in
`incidents/news_feeds.yaml` — the Texas political press, TV and public radio, the two state parties, and the
national desks — into `data/news.sqlite` (synced to the bucket), which keeps every article it has seen.
`incidents/outlets.yaml` gives each outlet a lean (left / lean-left / center / lean-right / right; a judgment,
edit freely) and marks the Texas outlets. The news step counts an article for a claim only when it is about
the claim: it names a specific entity of the claim (not the tracker's own subject) with another entity or
distinctive word, or any entity plus a distinctive word, or three distinctive words. Each claim's coverage
carries outlet counts by lean and a one-sided flag; the claims page shows them and the insights news block
reads how many claims were carried on one side of the press only.

## Scorecard

Every run, `features` logs each tracker's ten "likely to grow" picks to `data/features/picks.csv` (rank, heuristic,
whether the hub's "Gaining now" list showed it). A day later the feature table has their outcome, and the insights
page scores them: how often the picks reached the tracker's top five within 24h against the base rate of every
claim competing that day, overall, by rank, and for the hub-shown picks only; snapshots older than the log are
backtested with the same heuristic. The verdict line holds until 50 picks are scored. This is the bar the growth
model has to beat (`tracker/scorecard.py`). Picks carry a `method` column: the growth model's picks are logged
beside the heuristic's every run (in shadow until the model earns the hub), and the scorecard shows both on
the same snapshots.

## Poll releases as events

Hand-entered events live in `incidents/events.yaml`; poll releases are also found automatically in the claims
(`tracker/pollevents.py`): a checkable fact that names a pollster and carries numbers, at 50K+ views, becomes a
poll event timed at its first post on X, one per pollster per release (claims within 36h sharing a pollster token
are one release, so "YouGov/Univision" and "Univision/YouGov" do not double up), applied to every tracker whose
subject the claim mentions (an Ohio poll quoted in a Texas thread is not a Texas event). A hand-entered poll
event within 24h naming the same pollster wins. They feed the chart's events band, the analysis page's response
table, claim event tags, the control score's burst exemption and the feature table like any other event.

## Account registry

`python run.py registry` (the job runs it before each pull, and `accounts --all` rebuilds it for the amplifier map)
writes `out/accounts/registry.json`: one row per account across every tracker with its record as an originator —
canonical claims it posted first and was among the first three on, how many were verified and how many held up
(reliability: supported share, 3+ verified), how often a claim it touched early reached the tracker's top five
within 24h against the base rate (early-touch lift, 5+ scored), suspicious link rings it posted in, followers,
account age and amplifier-map role. The amplifier map shows it under "Who to believe, who to follow"; the feature
table stamps each claim-day with its first poster's `first_reliability` and `first_early_lift`.

## Insights

`python run.py insights` (the job runs it before assembling the site) recomputes, across every tracker, the
correlations the data supports so far and writes `out/insights.json` and `out/insights/index.html`: who posts
first vs. reaching the tracker's top five within 24h; accounts whose early touch is followed by growth; event
type → lane response; lanes that lead other lanes (hourly views, best lag within ±12h, so the daily cycle does
not pose as a lead); where the unsupported and misleading claims live, by lane; pushback timing vs. reach;
control score vs. growth; which claims crossed into the news; the strongest lane-to-market relation. Every
block states its N and a reading written by rule from the numbers, not by a model, so it says the same thing
tomorrow with more data. A top-five outcome only counts on tracker-days where at least ten claims competed.

## Link rings

`python run.py rings incidents/x.yaml` (part of `all`, before `control`) groups every stored post by the links it
carries (tracking parameters stripped, X status links reduced to the post id) and flags a ring when five or more
distinct accounts shared the same link inside two hours. Reporters and poll accounts do that within the hour of a
story and that is news; a ring where a fifth of the accounts are under 90 days old or most have under 5K followers
is marked suspicious, shown red on `analysis.html`, and counts toward the control score ("link rings" feature).
Re-uploaded video clips can't be matched this way (no shared URL); same-words copies cover the caption side.

## News coverage (Google News)

`python run.py news incidents/x.yaml` (part of `all`, after verification) reads Google News search RSS (free, no key; the GDELT DOC API is the fallback) for
each tracker's biggest recent claims — 15 per run at 50K+ views, re-checked every 12 hours, one query built from
the claim's entities and its most distinctive word — and records how many outlets and articles carried it in the
24 and 48 hours after the claim first appeared on X, when the first article ran, and whether the press had it
before X did. `claims.html` shows a News column and an "In the news" filter (3+ outlets within 48h is the
crossover outcome the feature table records as `news_sources_48h`). Google's feed reaches back seven days; the
RSS article store (above) reaches back to its first article, so the 48 hours after a claim's first post can be
measured for any claim first seen after the store began. A claim whose window neither source covers is not
checked, and an earlier check of such a claim shows "48h window not covered" rather than a zero. News lookups
are best-effort: when neither source answers, the rest of the list waits for the next run.

## Narrative control

`python run.py control incidents/x.yaml` (part of `all`, before `analysis`) scores every lane and every
canonical claim with five or more posts for how much it looks like a push rather than organic spread, 0–100:
copies across accounts (25), new accounts under 90 days old (20), burst — share of posts in the busiest
hour (15), regularity — copies spaced on a clock (15), top-5 account share of views (10; ignored under ten
accounts), and link rings of new or small accounts (15). The features are shown next to the score on `analysis.html`; anything at 70+ is listed on the
hub as a coordinated push. A poll release bursts and gets copied on its own, so read the score with the
events behind it (ROADMAP: events calendar).

## Events calendar

`incidents/events.yaml` is the hand-kept list of what happens off X — poll releases, rallies, hearings,
votes, rulings, debates, ad buys, endorsements, calendar dates — each with a local time (or a date for an
all-day event), a type, the trackers it bears on and where the time comes from. It feeds four things:
markers on every chart; the "Events and how the lanes answered" table on `analysis.html` (views in the 12
hours after vs. the 12 before, per lane); an "after: <event>" tag on canonical claims that first appear
within 6 hours of one; and the control score, which stops counting a burst that follows an event. Times
taken from the first post on X are a lower bound for the moment itself — put the real time in when known.

## Prediction features

`python run.py features incidents/x.yaml` (part of `all`) appends one row per lane and per canonical claim
to `data/features/<slug>.csv` every run: age, posts, views, accounts, copies, first poster's views and
followers, views in the last 1/6/24 hours, 6-hour momentum, control score, new-account share, burst,
whether it followed a calendar event, and the Kalshi price and its 24h change. The outcome columns
(`gain_24h`, `gain_72h`, `top5_24h`) are filled in by later runs once the store reaches 24 and 72 hours past
the row's moment, so after two or three weeks the file is a labelled training set. Until then, a heuristic
(6h views × momentum × accounts, unexplained bursts up-weighted) ranks claims "likely to grow" into
`out/<slug>/features.json`, and the hub shows the strongest across trackers as "Gaining now".

## Growth model

`python run.py growth` (the job runs it before the per-tracker loop and again at assembly) fits a model on the
feature tables for the outcome the scorecard measures: will this claim be in its tracker's top five by views
gained over the next 24 hours, on snapshots where ten or more claims competed. Features are the row's own
numbers (views, accounts, momentum, control, burst, event-follow, press coverage, the first poster's registry
stats, the heuristic score itself) and the claim's standing inside its snapshot — rank and share by six-hour
views, rank by total views, momentum and accounts, field size — because the outcome is a rank. Logistic
regression and gradient boosting are tried; validation is time-ordered with a day's gap (train on snapshots
before the cutoff, test on the newest quarter of claim-days starting 24 hours after it, so no training label
is computed from the test period). The report (`out/growth/model.json`) gives, for the heuristic and each
learner, the number that matters — of the five claims each ranks highest per snapshot, how many hit — plus
AUC, Brier, calibration, the top features, and how many hits were breakouts (not already top five by views).

The switch is earned, not assumed: the model drives the hub's "Gaining now" list only when it beats the
heuristic's precision@5 by 10 points across at least 20 held-out snapshots (`use_model` in the report). Until
then the heuristic keeps the hub and `features` logs the model's picks in shadow, so the scorecard (insights
section 0) shows both methods live on the same snapshots; section 12 carries the report. First fit (Oct 19,
26,343 labelled claim-days over four days): the model's top-five picks hit 38–40% against the heuristic's 20%
on 13 test snapshots, AUC 0.89 vs 0.59, with most of its hits claims that were not yet leaders — enough edge,
not yet enough snapshots. The fitted model is pickled to `out/growth/model.pkl` for the same job's
`features` step; nothing in `out/growth/` is committed.

## Next

See `ROADMAP.md`: narrative-control score, human corrections as few-shot examples, an events calendar
with event-to-lane correlation, verification of checkable claims, and a prediction feature table.
