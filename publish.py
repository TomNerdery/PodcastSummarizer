#!/usr/bin/env python3
"""
Upload the podcast to Cloudflare R2 (S3-compatible).

Pushes new episode MP3s, the cover image, and podcast.xml to your R2 bucket,
which Spotify then reads. MP3s already in the bucket are skipped unless --force.

Credentials are read from .env / environment (never hard-coded):
    R2_ACCOUNT_ID=...           # the 32-char account id from the R2 dashboard
    R2_ACCESS_KEY_ID=...        # from an R2 "Object Read & Write" API token
    R2_SECRET_ACCESS_KEY=...
    R2_BUCKET=podcast           # your bucket name
    R2_PUBLIC_BASE=https://pub-xxxx.r2.dev   # the bucket's public URL (r2.dev or custom domain)

Setup:
    pip install boto3

Usage:
    python3 publish.py            # sync new files
    python3 publish.py --force    # re-upload every MP3
    python3 publish.py --dry-run  # show what would upload, change nothing
"""

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from assemble import episode_mp3s
from tts_elevenlabs import load_dotenv

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
EPISODES_DIR = DATA_DIR / "episodes"
FEED = HERE / "podcast.xml"
COVER_CANDIDATES = ["cover.jpg", "cover.jpeg", "cover.png"]

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".xml": "application/rss+xml",
}


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set (add it to .env).")
    return val


def make_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("ERROR: boto3 not installed. Run:  pip install boto3")
    account = env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", region_name="auto"),
    )


def needs_upload(client, bucket: str, key: str, path: Path) -> bool:
    """True unless the bucket already holds this exact file.

    Checking presence alone is not enough. Reprocessing a video on the same day
    regenerates the same "<date>-<slug>.mp3" filename with different audio, so a
    presence-only check left R2 serving the previous take while the feed
    advertised the new byte count. Comparing size costs nothing (head_object
    returns it) and catches every real case.
    """
    from botocore.exceptions import ClientError
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError:
        return True
    return head.get("ContentLength") != path.stat().st_size


def upload(client, bucket: str, path: Path, key: str, dry: bool) -> None:
    ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    print(f"  {'(dry) ' if dry else ''}upload {key}  [{ctype}, {path.stat().st_size/1024:.0f} KB]")
    if not dry:
        client.upload_file(str(path), bucket, key,
                           ExtraArgs={"ContentType": ctype})


def main() -> None:
    p = argparse.ArgumentParser(description="Upload podcast to Cloudflare R2")
    p.add_argument("--force", action="store_true", help="Re-upload all MP3s")
    p.add_argument("--dry-run", action="store_true", help="Show actions without uploading")
    args = p.parse_args()

    load_dotenv(HERE / ".env")
    bucket = env("R2_BUCKET")
    public_base = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")
    client = make_client()

    # 1. Episode MP3s (skip ones the bucket already has byte-for-byte).
    # Narration masters stay on the volume; only finished episodes ship.
    mp3s = episode_mp3s(EPISODES_DIR)
    new_count = 0
    for mp3 in mp3s:
        key = f"episodes/{mp3.name}"
        if not args.force and not needs_upload(client, bucket, key, mp3):
            continue
        upload(client, bucket, mp3, key, args.dry_run)
        new_count += 1

    # 2. Cover art (always refresh if present).
    for name in COVER_CANDIDATES:
        cover = DATA_DIR / name
        if cover.exists():
            upload(client, bucket, cover, name, args.dry_run)
            break

    # 3. Spend figures, for the lab dashboard. Generated here rather than as a
    # separate pipeline step, so adding it needs no change to the CronJob
    # command and therefore no cluster edit.
    #
    # PUBLIC, deliberately and with Michael's agreement: this bucket already
    # serves every episode and the whole feed, and the tile is only useful if a
    # browser on the LAN can read it. It carries counts, characters, credits and
    # dollars. It must never carry a key, a URL with a token in it, or anything
    # per-listener.
    try:
        import json as _json
        from tts_report import summarise
        episodes = _json.loads((DATA_DIR / "episodes.json").read_text(encoding="utf-8"))
        stats = summarise(episodes)
        stats["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        stats_path = DATA_DIR / "stats.json"
        stats_path.write_text(_json.dumps(stats, indent=2), encoding="utf-8")
        upload(client, bucket, stats_path, "stats.json", args.dry_run)
    except Exception as e:
        # A dashboard tile must never be able to stop the feed publishing.
        print(f"  (stats.json skipped: {e})", file=sys.stderr)

    # 4. Feed last, so it only points at files already uploaded.
    if FEED.exists():
        upload(client, bucket, FEED, "podcast.xml", args.dry_run)
    else:
        print("  WARNING: podcast.xml not found — run build_feed.py first.", file=sys.stderr)

    print(f"\n{new_count} new MP3(s) uploaded.")
    if public_base:
        print(f"Feed URL:  {public_base}/podcast.xml")
        print("Submit that URL once in Spotify for Creators (add existing podcast / use own RSS).")
    else:
        print("Set R2_PUBLIC_BASE in .env to print your public feed URL.")


if __name__ == "__main__":
    main()
