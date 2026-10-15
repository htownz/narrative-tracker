"""The stores live in an S3-compatible bucket, not in git.

Each run of the job downloads every store at the start and uploads what changed at the end; once a week it also
writes a dated snapshot. Works with any S3-compatible provider (Cloudflare R2, Railway Buckets, AWS S3,
Backblaze B2) through five environment variables (or .env):

  BUCKET_ENDPOINT      https://<account>.r2.cloudflarestorage.com   (empty for AWS S3 proper)
  BUCKET_NAME          narrative-tracker
  BUCKET_ACCESS_KEY    …
  BUCKET_SECRET_KEY    …
  BUCKET_REGION        auto (default)

    python -m tracker.bucket pull       # download stores that are missing or older than the bucket's copy
    python -m tracker.bucket push       # upload stores whose content changed since the last push
    python -m tracker.bucket snapshot   # copy every store to snapshots/YYYY-MM-DD/
    python -m tracker.bucket list       # what the bucket holds

Layout in the bucket:  stores/<slug>.sqlite   snapshots/<date>/<slug>.sqlite   features/<slug>.csv
Without the variables set, every command is a no-op that says so (the job then keeps committing stores to git).
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, env

PREFIX_STORES = "stores/"
PREFIX_SNAPS = "snapshots/"
PREFIX_FEATURES = "features/"
SNAPSHOT_KEEP = 8          # dated snapshots kept (weekly -> about two months)


def configured() -> bool:
    return bool(env("BUCKET_NAME") and env("BUCKET_ACCESS_KEY") and env("BUCKET_SECRET_KEY"))


def _xfer():
    # single-part uploads keep the ETag equal to the file's MD5, which is how pull/push skip unchanged files
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=1 << 30, multipart_chunksize=1 << 30)


def _client():
    import boto3
    from botocore.config import Config
    endpoint = env("BUCKET_ENDPOINT") or None
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name=env("BUCKET_REGION") or "auto",
        aws_access_key_id=env("BUCKET_ACCESS_KEY"), aws_secret_access_key=env("BUCKET_SECRET_KEY"),
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                      s3={"addressing_style": "virtual" if endpoint and "storageapi" in endpoint else "auto"}),
    )


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_files() -> list[tuple[str, Path]]:
    """(bucket key, local path) for every store and feature file on disk."""
    out = [(PREFIX_STORES + p.name, p) for p in sorted(DATA_DIR.glob("*.sqlite"))]
    out += [(PREFIX_FEATURES + p.name, p) for p in sorted((DATA_DIR / "features").glob("*.csv"))]
    return out


def _remote(client, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for o in resp.get("Contents", []) or []:
            out[o["Key"]] = {"size": o["Size"], "etag": (o.get("ETag") or "").strip('"'), "modified": o["LastModified"]}
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")


def pull() -> int:
    """Download stores and feature files the bucket has that we lack or that differ from ours. The bucket wins:
    it holds what the last run left, which is newer than anything checked out of git."""
    if not configured():
        print("bucket: not configured (BUCKET_* unset) — using the stores on disk")
        return 0
    c, bucket = _client(), env("BUCKET_NAME")
    remote = {**_remote(c, bucket, PREFIX_STORES), **_remote(c, bucket, PREFIX_FEATURES)}
    if not remote:
        print("bucket: empty — nothing to pull (the stores on disk will seed it at the end of this run)")
        return 0
    local = dict(_local_files())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "features").mkdir(parents=True, exist_ok=True)
    n = 0
    for key, meta in remote.items():
        dest = DATA_DIR / key[len(PREFIX_STORES):] if key.startswith(PREFIX_STORES) else DATA_DIR / "features" / key[len(PREFIX_FEATURES):]
        if key in local and dest.exists() and dest.stat().st_size == meta["size"] and _md5(dest) == meta["etag"]:
            continue
        c.download_file(bucket, key, str(dest), Config=_xfer())
        n += 1
    print(f"bucket: pulled {n} of {len(remote)} files ({sum(m['size'] for m in remote.values()) / 1e6:.1f} MB in the bucket)")
    return n


def push() -> int:
    """Upload every store and feature file whose content differs from the bucket's copy."""
    if not configured():
        print("bucket: not configured — stores stay in git")
        return 0
    c, bucket = _client(), env("BUCKET_NAME")
    remote = {**_remote(c, bucket, PREFIX_STORES), **_remote(c, bucket, PREFIX_FEATURES)}
    n = total = 0
    for key, path in _local_files():
        total += 1
        r = remote.get(key)
        if r and r["size"] == path.stat().st_size and r["etag"] == _md5(path):
            continue
        c.upload_file(str(path), bucket, key, Config=_xfer())
        n += 1
    print(f"bucket: pushed {n} of {total} files")
    return n


def snapshot(keep: int = SNAPSHOT_KEEP) -> int:
    """Copy today's stores to snapshots/<date>/ (server-side copy), dropping the oldest beyond `keep` dates."""
    if not configured():
        print("bucket: not configured — no snapshot")
        return 0
    c, bucket = _client(), env("BUCKET_NAME")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stores = _remote(c, bucket, PREFIX_STORES)
    for key in stores:
        c.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key}, Key=f"{PREFIX_SNAPS}{day}/{key[len(PREFIX_STORES):]}")
    dates = sorted({k.split("/")[1] for k in _remote(c, bucket, PREFIX_SNAPS) if k.count("/") >= 2})
    for old in dates[:-keep] if len(dates) > keep else []:
        for k in _remote(c, bucket, f"{PREFIX_SNAPS}{old}/"):
            c.delete_object(Bucket=bucket, Key=k)
    print(f"bucket: snapshot {day} with {len(stores)} stores; {min(len(dates), keep)} snapshots kept")
    return len(stores)


def listing() -> None:
    if not configured():
        print("bucket: not configured")
        return
    c, bucket = _client(), env("BUCKET_NAME")
    for prefix in (PREFIX_STORES, PREFIX_FEATURES, PREFIX_SNAPS):
        items = _remote(c, bucket, prefix)
        print(f"{prefix}  {len(items)} files, {sum(m['size'] for m in items.values()) / 1e6:.1f} MB")
        for k, m in sorted(items.items())[:40]:
            print(f"   {m['size'] / 1e6:7.1f} MB  {m['modified'].strftime('%Y-%m-%d %H:%M')}  {k}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    {"pull": pull, "push": push, "snapshot": snapshot, "list": listing}[cmd]()
