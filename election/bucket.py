"""Optional off-git copies of the election store and raw feed archive, in the same S3-compatible bucket the
tracker uses (tracker/bucket.py; BUCKET_* variables). A no-op when the bucket is not configured.

Layout:  election/election-2026.sqlite        election/raw/<stamp>_<version>.json.gz
"""
from __future__ import annotations

import sys
from pathlib import Path

from tracker import bucket as tb
from tracker.config import env

from . import DATA_DIR

PREFIX = "election/"


def push_raw() -> int:
    """Upload raw archives the bucket lacks."""
    if not tb.configured():
        return 0
    c, b = tb._client(), env("BUCKET_NAME")
    have = tb._remote(c, b, PREFIX + "raw/")
    n = 0
    for p in sorted((DATA_DIR / "raw").glob("*.json.gz")):
        key = PREFIX + "raw/" + p.name
        if key in have and have[key]["size"] == p.stat().st_size:
            continue
        c.upload_file(str(p), b, key, Config=tb._xfer())
        n += 1
    return n


def push_store() -> bool:
    if not tb.configured():
        return False
    c, b = tb._client(), env("BUCKET_NAME")
    for p in DATA_DIR.glob("*.sqlite"):
        c.upload_file(str(p), b, PREFIX + p.name, Config=tb._xfer())
    return True


def pull_store() -> bool:
    """Download the store if the bucket's copy is newer than the checkout's (a killed job may have pushed there last)."""
    if not tb.configured():
        return False
    c, b = tb._client(), env("BUCKET_NAME")
    remote = tb._remote(c, b, PREFIX)
    got = False
    for key, meta in remote.items():
        if not key.endswith(".sqlite"):
            continue
        dest = DATA_DIR / key[len(PREFIX):]
        if dest.exists() and dest.stat().st_size == meta["size"] and tb._md5(dest) == meta["etag"]:
            continue
        if dest.exists() and dest.stat().st_mtime > meta["modified"].timestamp():
            continue
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        c.download_file(b, key, str(dest), Config=tb._xfer())
        got = True
    return got


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if not tb.configured():
        print("election bucket: not configured (BUCKET_* unset)")
        sys.exit(0)
    if cmd == "push":
        print(f"election bucket: raw +{push_raw()}, store {'pushed' if push_store() else 'skipped'}")
    elif cmd == "pull":
        print(f"election bucket: store {'pulled' if pull_store() else 'up to date'}")
    else:
        c, b = tb._client(), env("BUCKET_NAME")
        for k, m in sorted(tb._remote(c, b, PREFIX).items()):
            print(f"  {m['size'] / 1e6:7.2f} MB  {m['modified'].strftime('%Y-%m-%d %H:%M')}  {k}")
