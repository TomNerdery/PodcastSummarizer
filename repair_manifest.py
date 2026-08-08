#!/usr/bin/env python3
"""
Recover episodes whose audio exists but never made it into episodes.json, and
retro-fit the intro/outro stings onto episodes produced before assemble.py.

Why this exists: if the runner dies between paying ElevenLabs and recording the
episode, the MP3 sits on disk, fully paid for, and the feed never mentions it.
This finds those, matches them back to their source video, and files them.

Matching is by filename. The runner names episodes "<date>-<slugified-title>.mp3",
so the slug is re-derived from the playlist's titles and compared.

    python3 repair_manifest.py --dry-run     # show what it would do
    python3 repair_manifest.py               # backfill the manifest
    python3 repair_manifest.py --wrap        # ...and add stings to bare episodes

After --wrap, run:  python3 build_feed.py && python3 publish.py --force
(--force because publish skips MP3s already in the bucket, and wrapping rewrites
them.)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from assemble import build_episode, have_stings
from runner import EPISODES_DIR, MANIFEST, load_json, save_json, slugify
from tts_elevenlabs import load_dotenv

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
YT_API = "https://www.googleapis.com/youtube/v3/playlistItems"


def playlist_entries(playlist_id: str) -> dict:
    """Map slugified-title -> {video_id, title, channel} for a playlist."""
    load_dotenv(HERE / ".env")
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("ERROR: YOUTUBE_API_KEY is not set (needed to match episodes back to videos).")
    out, page = {}, None
    while True:
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id,
                  "maxResults": 50, "key": key}
        if page:
            params["pageToken"] = page
        r = requests.get(YT_API, params=params, timeout=30)
        if r.status_code != 200:
            sys.exit(f"YouTube API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        for item in data.get("items", []):
            snip = item.get("snippet", {})
            title = snip.get("title", "")
            if not title:
                continue
            out[slugify(title)] = {
                "video_id": item["contentDetails"]["videoId"],
                "title": title,
                "channel": snip.get("videoOwnerChannelTitle") or snip.get("channelTitle", ""),
            }
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def processed_entries(claimed: set) -> dict:
    """Same map, rebuilt from processed.json for videos no longer in the playlist.

    A video dropped from the queue still has paid-for audio on disk; without this
    it can never be matched back to a title and stays unpublished forever.
    oEmbed is free and needs no API key.
    """
    import urllib.parse
    import urllib.request

    ids = load_json(DATA_DIR / "processed.json", {"ids": []}).get("ids", [])
    out = {}
    for vid in ids:
        if vid in claimed:
            continue
        watch = f"https://www.youtube.com/watch?v={vid}"
        url = ("https://www.youtube.com/oembed?url="
               + urllib.parse.quote(watch, safe="") + "&format=json")
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue  # deleted or private video; nothing more we can do
        title = data.get("title", "")
        if title:
            out[slugify(title)] = {
                "video_id": vid,
                "title": title,
                "channel": data.get("author_name", ""),
            }
    return out


def split_name(mp3: Path) -> tuple[str, str]:
    """'2026-08-07-some-title.mp3' -> ('2026-08-07', 'some-title')."""
    stem = mp3.stem
    if len(stem) > 11 and stem[:10].count("-") == 2 and stem[10] == "-":
        return stem[:10], stem[11:]
    return "", stem


def has_stings(entry: dict) -> bool:
    return bool(entry.get("assembled"))


def wrap_in_place(mp3: Path) -> bool:
    """Add the stings to an existing episode. Leaves the original alone on any doubt."""
    if not have_stings():
        return False
    before = mp3.stat().st_size
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "wrapped.mp3"
        build_episode(mp3, staged)
        if not staged.exists() or staged.stat().st_size <= before * 0.9:
            # Shorter output means the assembly quietly fell back or went wrong.
            print(f"    skipped {mp3.name}: output looked wrong, original untouched")
            return False
        shutil.copyfile(staged, mp3)
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill orphaned episodes into the manifest")
    p.add_argument("--playlist", default=os.environ.get("PLAYLIST_ID", ""),
                   help="Playlist to match titles against (default: $PLAYLIST_ID)")
    p.add_argument("--wrap", action="store_true",
                   help="Also add intro/outro stings to episodes that lack them")
    p.add_argument("--dry-run", action="store_true", help="Report only, change nothing")
    args = p.parse_args()

    episodes = load_json(MANIFEST, [])
    known = {Path(e.get("mp3_file", "")).name for e in episodes}
    on_disk = sorted(EPISODES_DIR.glob("*.mp3")) if EPISODES_DIR.exists() else []
    orphans = [m for m in on_disk if m.name not in known]

    print(f"Manifest: {len(episodes)} episode(s)")
    print(f"On disk:  {len(on_disk)} MP3(s)")
    print(f"Orphaned: {len(orphans)} MP3(s) paid for but never published\n")

    added = 0
    if orphans:
        if not args.playlist:
            sys.exit("ERROR: need --playlist (or $PLAYLIST_ID) to match orphans to videos.")
        print("Fetching playlist titles to match against...")
        lookup = playlist_entries(args.playlist)
        print(f"  {len(lookup)} video(s) in the playlist\n")

        # Anything the playlist cannot explain gets a second pass against
        # processed.json, which remembers videos since removed from the queue.
        if any(split_name(m)[1] not in lookup for m in orphans):
            claimed = {v["video_id"] for v in lookup.values()}
            extra = processed_entries(claimed)
            if extra:
                print(f"  {len(extra)} more title(s) recovered from processed.json\n")
            lookup.update(extra)

        for mp3 in orphans:
            date, slug = split_name(mp3)
            meta = lookup.get(slug)
            script = mp3.with_suffix(".txt")
            if not meta:
                print(f"  UNMATCHED  {mp3.name}")
                print("             (source video is gone; left on disk, not published)")
                continue
            entry = {
                "video_id": meta["video_id"],
                "title": meta["title"],
                "channel": meta["channel"],
                "source_url": f"https://www.youtube.com/watch?v={meta['video_id']}",
                "date": date,
                "voice": "",
                "script_file": str(script.relative_to(DATA_DIR)) if script.exists() else "",
                "mp3_file": str(mp3.relative_to(DATA_DIR)),
                "summary_words": len(script.read_text(encoding="utf-8").split())
                                 if script.exists() else 0,
                "backfilled": True,
            }
            print(f"  RECOVERED  {date}  {meta['title'][:60]}")
            # Appended even on a dry run so the --wrap count below is honest;
            # only save_json() is gated.
            episodes = [e for e in episodes if e.get("video_id") != entry["video_id"]]
            episodes.append(entry)
            added += 1

    wrapped = 0
    if args.wrap:
        print("\nAdding stings to episodes that lack them...")
        if not have_stings():
            print(f"  no stings found in {DATA_DIR / 'assets'}; run make_stings.py first")
        else:
            for entry in episodes:
                if has_stings(entry):
                    continue
                mp3 = DATA_DIR / entry.get("mp3_file", "")
                if not mp3.exists():
                    continue
                print(f"  {mp3.name}")
                if args.dry_run:
                    wrapped += 1
                    continue
                if wrap_in_place(mp3):
                    entry["assembled"] = True
                    wrapped += 1

    if not args.dry_run:
        save_json(MANIFEST, episodes)

    print(f"\n{'(dry run) ' if args.dry_run else ''}"
          f"Recovered {added} episode(s), wrapped {wrapped}. "
          f"Manifest now holds {len(episodes)}.")
    if not args.dry_run and (added or wrapped):
        print("Next: python3 build_feed.py && python3 publish.py"
              f"{' --force' if wrapped else ''}")


if __name__ == "__main__":
    main()
