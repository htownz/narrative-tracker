# Election layer — results, and the dashboard built on them

The narrative tracker already answers *who is saying what, who is pushing it, and whether it is true*. This
layer adds *what the voters did*: live returns on election night, then the canvassed results, then what they
mean for the 90th Legislature and for Washington, so the firm and its clients can go from a county map to a
narrative lane to a committee chair in one place. The firm's own election report is both an input (its framing of
which races matter) and an output (its result tables are generated from this store).

Built the evening of Nov 3, 2026, starting with the plumbing. Everything under "Tonight" exists and runs;
everything under "This week" is the plan.

## Tonight: the plumbing

```
Texas SOS ENR (Civix)  ─┐
Harris / Tarrant PDFs   ├─►  python -m election snapshot  ─►  data/election/election-2026.sqlite  ─►  out/election/*.json  ─►  dashboard
Travis Clarity JSON     │        (every 5 min, GitHub Actions)        (snapshots, deltas)                (latest, series,
Bexar Electionware HTML ┘                                                                                 counties, status)
```

**Sources** (all public, all free, all official):

| Source | What | How |
|---|---|---|
| Texas Secretary of State election-night returns | every federal, statewide and districted contest (227 tracked of 584), plus all 254 counties: registered voters, votes cast, precincts and polling places reporting, every contest's county vote | `election/sos.py` — the Civix IVIS JSON behind results.texas-election.com: `electionConstants` → election 53815 → `election/53815` (statewide) and `election/countyInfo/53815` (counties). Payloads are base64-wrapped JSON. Feed says it refreshes every 5 minutes; its `Version` and `LastUpdatedTime` drive change detection. |
| Harris County, Tarrant County | county judge, commissioners (and 180+ other local contests) | `election/county.py` — Hart Verity "Cumulative Results Report" PDFs, parsed with `pdftotext -layout` (contest headers, candidate rows, precincts reporting, run time) |
| Travis County | Austin City Council districts 1, 3, 5, 8, 9 | Scytl/Clarity ENR: `elections.json` → the Nov 3 election's EID → `current_ver.txt` → `json/en/summary.json` |
| Bexar County | county judge, commissioners | Electionware summary HTML (`home.bexar.org/el45a.html`), read as a token stream; the page still showed Nov 2024 at 5:30 PM, so the parser is best-effort until the 2026 report posts |

Not used: electionenjoyer.com (a simulator; its live mode reads the same SOS backend through a Cloudflare relay),
AP / Decision Desk (paid; their race calls can be recorded by hand with `python -m election call`).

**Race registry** (`election/registry.yaml`): the SOS feed supplies contests and candidates; the YAML overlays the
firm report's framing — the 21 competitive House districts (3 open), the 16 Senate seats with the five notable
ones, the 16 congressional races, open seats, regions, notes — plus the county-run races with the regex that
matches them in each county's report, the report's focus counties, and the turnout benchmarks (19,041,684
registered; 7.73 M early; 8.37 M / 53.0 % in 2018). Prior-cycle `baseline:` figures are placeholders typed from
memory and flagged `baselines_verified: false` until checked against the SOS canvass.

**Store** (`election/store.py`, `data/election/election-2026.sqlite`, committed hourly by the job):

- `polls` — every fetch (5-minute heartbeat) and whether the feed had changed
- `snapshots` — one row per feed change: fetch time, SOS update time, version, counties / precincts / polling places reporting
- `races`, `candidates` — the tracked contests and who is in them (key, title, chamber, competitive, open seat, region, report section, note)
- `race_votes`, `race_reporting` — statewide totals per candidate per snapshot (the time series for every race)
- `county_votes` — county × race × candidate rows, written only when a county's numbers change (deltas keep the night to tens of thousands of rows)
- `county_turnout` — per county per change: registered, votes cast, precincts and polling places reporting
- `local_votes`, `local_polls` — the county-run races, per report change, with each county source's status
- `calls` — race calls recorded by hand (`python -m election call sd-9 Rehmet --by AP`)

Raw decoded feeds are archived gzipped under `data/election/raw/` (gitignored; copied to the S3 bucket when
`BUCKET_*` is set) so anything can be recomputed.

**Exports** (`election/export.py` → `out/election/`, committed every 30 minutes):

- `latest.json` — every race as of the newest snapshot: candidates sorted by votes with pct, leader, margin, precincts and counties reporting, baseline, call, the report framing
- `series.json` — per race, one row per snapshot: `[fetched_at, total, precincts_pct, leader_party, margin_pct, {R, D, O}]`
- `counties.json` — newest county rows for the top five statewide races (R/D/other votes, early vs election day, margin) and county turnout
- `status.json` — store counts, last poll, last change, each county source's state

The public repo serves these at `https://raw.githubusercontent.com/htownz/narrative-tracker/main/out/election/`;
the GitHub Pages site will carry them once the dashboard page is built.

**Job** (`.github/workflows/election-night.yml`): one run polls for ~5.5 hours (5-minute cadence), commits
`out/election/` every 30 minutes and the store every hour; the half-hourly schedule queues the next run behind it
so coverage is continuous through Nov 6. A commit to `election/.run` restarts polling with new code (and cancels
the live run, so push right after its last commit landed); a `stop` line in that file ends the night.

**CLI**: `python -m election probe | snapshot | watch | export | status | races [--competitive] | call <key> <name> --by AP`.

## This week: the dashboard

One page per layer, all reading the same store and the tracker's existing outputs, published with the rest of
the site under `/election/`. Order is the build order; each layer is useful on its own.

1. **Results** (`/election/`) — the live board: statewide, the competitive legislative and congressional seats, the
   county races; margin vs the registry baseline; precincts and counties reporting; a county map for the top
   races (TopoJSON of the 254 counties, shaded by margin, with swing vs 2024); a time scrubber over
   `series.json` to replay the night the way the narrative charts replay a story; the SOS canvass replaces the
   election-night numbers when it posts.
2. **Turnout** — statewide and by county against 2018 and the early-vote table in the report; where turnout rose
   and fell relative to registration; the focus counties first.
3. **Money** — what the report already carries (AdImpact and TEC figures per race) next to the margin, so
   dollars-per-point is visible; TEC filings for the legislative seats this week.
4. **Narrative** — straight from the tracker: for each race or issue, the lanes, the amplifier map, the control
   score, the verified claims, and "what electeds said" from the officials registry. The election-integrity
   tracker (`issue-election-integrity-live`) is the first one to un-pause: certification, Harris County, mail
   ballots and the "rigged" frame are the post-election story.
5. **Litigation** — a tracker of election-related suits and processes: CourtListener / RECAP (free API) for federal
   dockets, re:SearchTX for state courts, the AG's press releases, SOS notices, Democracy Docket and Election
   Law Blog as feeds. Each matter: court, parties, what is at stake, status, next date, the narrative lane it
   feeds. Stored like events (`incidents/events.yaml` style), shown on the results page next to the race it
   touches.
6. **Reactions** — the morning-after statements: Trump (Truth Social archive feed already wired), Abbott,
   Patrick, Burrows, Paxton/Talarico, the Texas delegation, via the officials registry and the news feeds;
   grouped by who is claiming the mandate for what.
7. **Implications** — the deliverable the clients open: for each of the firm's practice areas and for each
   client in `incidents/clients.yaml`, what the result changes — chairs and leadership (Speaker's race, the
   Lt. Governor's priorities), freshman count, the chamber splits, which bills from the issue trackers gain or
   lose sponsors, and the Washington side (the delegation after redistricting, committee seats). Written by
   Claude from the store plus the trackers, reviewed by the team, versioned.
8. **Report tables** — `python -m election report` fills the result tables in the firm's election report (every
   race in the report is a key in the registry), so the Nov 4 report is generated from the same numbers the
   dashboard shows.

Audience for now: Ben only. When it earns it: the team, then a client view filtered by `clients.yaml`.

## Still to decide

- Race calls: hand-recorded from AP tonight, or a conservative automatic rule (leader's margin larger than the
  votes plausibly outstanding, estimated from registered voters and the precincts not yet in) with the rule shown.
- Whether the SOS canvass (posted over the following weeks) and the 2018/2022/2024 county baselines come from
  the legacy `elections.sos.state.tx.us/elchist` pages or from the same Civix backend (it lists only 2025–2026
  elections today).
- Hosting the dashboard on GitHub Pages with the rest of the site vs the Netlify site the morning briefing uses.
