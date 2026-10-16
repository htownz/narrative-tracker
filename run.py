#!/usr/bin/env python3
"""Narrative tracker CLI.

  python run.py pull      incidents/x.yaml [--dry-run]   # search twitterapi.io, store posts
  python run.py discover  incidents/x.yaml [--accept]    # Claude proposes lanes; --accept writes them to the YAML
  python run.py classify  incidents/x.yaml [--relabel]   # put every post in a lane
  python run.py dedup     incidents/x.yaml                # find same-words clusters
  python run.py export    incidents/x.yaml                # write out/<slug>/data.json
  python run.py render    incidents/x.yaml                # write out/<slug>/index.html
  python run.py report    incidents/x.yaml                # top posts that fit no lane (add a lane if a story is emerging)
  python run.py video     incidents/x.yaml [--seconds 60] # MP4 play-through of the rendered chart (Playwright + ffmpeg)
  python run.py accounts  incidents/x.yaml                # amplifier map for one incident -> out/<slug>/accounts.html
  python run.py accounts  --all                           # amplifier map across every incident -> out/accounts/index.html
  python run.py analysis  incidents/x.yaml                # lane timing, pushback lag, market lead/lag -> out/<slug>/analysis.html
  python run.py alerts    incidents/x.yaml                # early warning: stories forming outside the lanes, lanes surging
  python run.py review    incidents/x.yaml                # lane spot-check page + overrides builder -> out/<slug>/review.html
  python run.py claims    incidents/x.yaml                # what is being claimed: extract + group claims -> out/<slug>/claims.html
  python run.py verify    incidents/x.yaml                # verdicts for big checkable claims (Claude + web search) -> stored, shown on claims.html
  python run.py news      incidents/x.yaml                # GDELT news coverage of the biggest claims -> news_checks (shown on claims.html)
  python run.py rings     incidents/x.yaml                # link rings: one URL pushed by 5+ accounts within 2h -> out/<slug>/rings.json (shown on analysis.html)
  python run.py control   incidents/x.yaml                # narrative-control score per lane and claim -> out/<slug>/control.json (shown on analysis.html)
  python run.py features  incidents/x.yaml                # prediction feature rows -> data/features/<slug>.csv; "likely to grow" -> out/<slug>/features.json
  python run.py compact   incidents/x.yaml                # slim the stored API objects and reclaim space (runs automatically at the start of `all`)
  python run.py brief [--days 7]                          # the weekly brief across every live tracker -> out/brief/<date>.html (+ Telegram)
  python run.py refresh-views incidents/x.yaml            # re-look-up the top 500 posts so view counts stop being first-pull values
  python run.py all       incidents/x.yaml                # pull -> (discover --accept if no lanes) -> classify -> dedup -> export -> render
  python run.py synth     incidents/x.yaml                # load a labelled synthetic sample so the chart can be developed without a key
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
for _stream in (sys.stdout, sys.stderr):   # Windows consoles default to a legacy code page; the logs use → and ¢
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tracker import dedup as dedup_mod  # noqa: E402
from tracker import export as export_mod  # noqa: E402
from tracker import lanes as lanes_mod  # noqa: E402
from tracker import pull as pull_mod  # noqa: E402
from tracker import render as render_mod  # noqa: E402
from tracker.config import load_incident, save_lanes  # noqa: E402
from tracker.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pull", "discover", "classify", "dedup", "export", "render", "report", "video", "accounts", "analysis", "alerts", "review", "claims", "verify", "news", "rings", "control", "features", "compact", "brief", "insights", "refresh-views", "all", "synth"])
    ap.add_argument("incident", nargs="?", default=None)
    ap.add_argument("--all", action="store_true", help="accounts: aggregate every incident into out/accounts/")
    ap.add_argument("--days", type=int, default=7, help="brief: length of the window in days (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="pull: print the search calls and stop")
    ap.add_argument("--accept", action="store_true", help="discover: write the proposed lanes into the YAML")
    ap.add_argument("--relabel", action="store_true", help="classify: re-label posts that already have a lane")
    ap.add_argument("--include-other", action="store_true", help="export: keep 'other' posts in the chart")
    ap.add_argument("--budget", type=float, default=None, help="pull: stop cleanly after this many seconds (resumable)")
    ap.add_argument("--seconds", type=float, default=60.0, help="video: play-through length")
    ap.add_argument("--fps", type=int, default=30, help="video: frames per second")
    ap.add_argument("--pause", type=float, default=None, help="pull: seconds between API calls (free tier needs 5.2)")
    args = ap.parse_args()

    if args.command == "brief":
        from tracker import brief
        brief.brief(days=args.days)
        return
    if args.command == "insights":
        from tracker import insights
        insights.insights()
        return
    if args.command == "accounts" and args.all:
        from tracker import accounts
        accounts.accounts_all()
        return
    if not args.incident:
        ap.error("incident YAML is required")
    inc = load_incident(args.incident)
    synthetic = False

    if args.command == "pull":
        ok = pull_mod.pull(inc, dry_run=args.dry_run, budget=args.budget, pause=args.pause)
        sys.exit(0 if ok else 3)
    elif args.command == "discover":
        lanes = lanes_mod.discover(inc)
        import yaml
        print(yaml.safe_dump({"lanes": lanes}, sort_keys=False, allow_unicode=True, width=100))
        if args.accept:
            save_lanes(inc, lanes)
            print(f"lanes written to {inc.path}")
    elif args.command == "classify":
        lanes_mod.classify(inc, relabel=args.relabel)
    elif args.command == "dedup":
        dedup_mod.dedup(inc)
    elif args.command == "export":
        synthetic = bool(Store(inc.db_path).get_meta("synthetic", False))
        export_mod.export(inc, include_other=args.include_other, synthetic=synthetic)
    elif args.command == "render":
        render_mod.render(inc)
    elif args.command == "report":
        lanes_mod.other_report(inc)
    elif args.command == "video":
        from tracker import video
        video.video(inc, seconds=args.seconds, fps=args.fps)
    elif args.command == "accounts":
        from tracker import accounts
        accounts.accounts(inc)
    elif args.command == "analysis":
        from tracker import analysis
        analysis.analysis(inc)
    elif args.command == "alerts":
        from tracker import alerts
        alerts.alerts_cmd(inc)
    elif args.command == "review":
        from tracker import review
        review.review(inc)
    elif args.command == "claims":
        from tracker import claims
        claims.claims(inc)
    elif args.command == "verify":
        from tracker import verify, claims
        verify.verify(inc)
        claims.claims(inc, skip_model=True)
    elif args.command == "news":
        from tracker import news, claims
        news.news(inc)
        claims.claims(inc, skip_model=True)
    elif args.command == "rings":
        from tracker import rings
        rings.rings(inc)
    elif args.command == "control":
        from tracker import control
        control.control(inc)
    elif args.command == "features":
        from tracker import features
        features.features(inc)
    elif args.command == "compact":
        st = Store(inc.db_path); n = st.compact(); st.close()
        print(f"compact: {n} rows slimmed; {inc.db_path.stat().st_size / 1e6:.1f} MB")
    elif args.command == "refresh-views":
        pull_mod.refresh_views(inc, pause=args.pause)
    elif args.command == "synth":
        from tracker import synth
        synth.load(inc)
        dedup_mod.dedup(inc)
        export_mod.export(inc, synthetic=True)
        render_mod.render(inc)
    elif args.command == "all":
        if inc.db_path.exists():
            st = Store(inc.db_path); n = st.compact(); seeded = st.seed_history(); st.close()
            if n:
                print(f"compact: {n} rows slimmed; store is {inc.db_path.stat().st_size / 1e6:.1f} MB")
            if seeded:
                print(f"view history seeded with {seeded} posts")
        if not pull_mod.pull(inc, budget=args.budget, pause=args.pause):
            print("pull paused on the time budget — continuing with what is stored; the next run resumes the pull", file=sys.stderr)
        if inc.live:
            try:   # the still-moving tail: posts 2-7 days old that the re-pull window no longer covers (2 calls, ~1.5 cents)
                pull_mod.refresh_views(inc, top_n=100, min_age_hours=48, max_age_hours=168, pause=args.pause)
            except SystemExit as e:
                print(f"[refresh-views] skipped: {e}", file=sys.stderr)
        if not inc.lanes:
            lanes = lanes_mod.discover(inc)
            save_lanes(inc, lanes)
            print("lanes discovered and written to the YAML:")
            for l in lanes:
                print(f"  - {l['key']}: {l['label']}")
        lanes_mod.classify(inc)
        dedup_mod.dedup(inc)
        export_mod.export(inc, include_other=args.include_other)
        render_mod.render(inc)
        from tracker import accounts, analysis, alerts, claims, control, features, news, review, rings, verify
        def claims_and_verify(inc):
            claims.claims(inc)                       # extract + cluster + page
            changed = bool(verify.verify(inc))
            try:
                changed = bool(news.news(inc)) or changed
            except Exception as e:  # GDELT is best-effort
                print(f"[news] failed: {type(e).__name__}: {e}", file=sys.stderr)
            if changed:
                claims.claims(inc, skip_model=True)  # page again with the new verdicts and news checks
        for name, fn in (("accounts", accounts.accounts), ("claims", claims_and_verify), ("rings", rings.rings), ("control", control.control),
                         ("features", features.features), ("analysis", analysis.analysis), ("review", review.review),
                         ("alerts", alerts.alerts_cmd), ("report", lanes_mod.other_report)):
            try:
                fn(inc)
            except Exception as e:  # best-effort extras; the chart and data are already written
                print(f"[{name}] failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
