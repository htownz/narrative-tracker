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
- White dot = 90%+ the same words as an earlier post (3-word shingles, Jaccard ≥ 0.90); white line back to the origin; "same words ×N" pill on origins with 3+ repeats.
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

## Next

- MP4 export: Playwright frame capture of the play-through, ffmpeg to stitch.
- Scheduled re-pulls so a live incident page keeps growing (the daily briefing task is the pattern).
- Account-level view: who originates, who amplifies, who copies.
