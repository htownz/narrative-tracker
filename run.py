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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pull", "discover", "classify", "dedup", "export", "render", "report", "video", "accounts", "analysis", "alerts", "review", "claims", "refresh-views", "all", "synth"])
    ap.add_argument("incident", nargs="?", default=None)
    ap.add_argument("--all", action="store_true", help="accounts: aggregate every incident into out/accounts/")
    ap.add_argument("--dry-run", action="store_true", help="pull: print the search calls and stop")
    ap.add_argument("--accept", action="store_true", help="discover: write the proposed lanes into the YAML")
    ap.add_argument("--relabel", action="store_true", help="classify: re-label posts that already have a lane")
    ap.add_argument("--include-other", action="store_true", help="export: keep 'other' posts in the chart")
    ap.add_argument("--budget", type=float, default=None, help="pull: stop cleanly after this many seconds (resumable)")
    ap.add_argument("--seconds", type=float, default=60.0, help="video: play-through length")
    ap.add_argument("--fps", type=int, default=30, help="video: frames per second")
    ap.add_argument("--pause", type=float, default=None, help="pull: seconds between API calls (free tier needs 5.2)")
    args = ap.parse_args()

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
        from tracker.store import Store
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
    elif args.command == "refresh-views":
        pull_mod.refresh_views(inc, pause=args.pause)
    elif args.command == "synth":
        from tracker import synth
        synth.load(inc)
        dedup_mod.dedup(inc)
        export_mod.export(inc, synthetic=True)
        render_mod.render(inc)
    elif args.command == "all":
        if not pull_mod.pull(inc, budget=args.budget, pause=args.pause):
            print("pull paused on the time budget — continuing with what is stored; the next run resumes the pull", file=sys.stderr)
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
        from tracker import accounts, analysis, alerts, claims, review
        for name, fn in (("accounts", accounts.accounts), ("analysis", analysis.analysis), ("claims", claims.claims),
                         ("review", review.review), ("alerts", alerts.alerts_cmd), ("report", lanes_mod.other_report)):
            try:
                fn(inc)
            except Exception as e:  # best-effort extras; the chart and data are already written
                print(f"[{name}] failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
