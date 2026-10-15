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
(hub at the root, each tracker under `/<slug>/`, the cross-tracker amplifier map under `/accounts/`).
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
`refresh_days` days, which are re-pulled each run. Once a week (the Sunday 6:17 AM CT run) `refresh-views`
re-looks-up each tracker's 500 most-viewed posts (10 calls, about 8 cents) so the big numbers stay current.

## Tests

`pip install pytest && pytest -q` — dedup rule, window and DST math, tweet flattening, store upserts, lane save,
overrides. `.github/workflows/test.yml` runs them on Ubuntu and Windows, Python 3.10 and 3.12, plus a synthetic
end-to-end render, on every push that touches code.

## Run health

The hub shows the twitterapi.io balance (red under $1), the last run time, and any live tracker with no lanes,
unlabelled posts, a partial pull or query errors. `site/status.json` carries the same data for scripts.

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

## Link rings

`python run.py rings incidents/x.yaml` (part of `all`, before `control`) groups every stored post by the links it
carries (tracking parameters stripped, X status links reduced to the post id) and flags a ring when five or more
distinct accounts shared the same link inside two hours. Reporters and poll accounts do that within the hour of a
story and that is news; a ring where a fifth of the accounts are under 90 days old or most have under 5K followers
is marked suspicious, shown red on `analysis.html`, and counts toward the control score ("link rings" feature).
Re-uploaded video clips can't be matched this way (no shared URL); same-words copies cover the caption side.

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

## Next

See `ROADMAP.md`: narrative-control score, human corrections as few-shot examples, an events calendar
with event-to-lane correlation, verification of checkable claims, and a prediction feature table.
