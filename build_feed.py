#!/usr/bin/env python3
"""
Build a podcast RSS feed (Spotify/Apple-compatible) from episodes.json.

This is the self-hosted route: you host the MP3s + cover image + podcast.xml
on any static host (Cloudflare R2, AWS S3, GitHub Pages, Netlify...), then
submit the podcast.xml URL once to Spotify for Creators. Spotify re-checks the
feed and pulls new episodes automatically after that.

Setup:
    pip install mutagen        # optional, for episode durations

Usage:
    python3 build_feed.py --init        # scaffold podcast.json (edit it once)
    python3 build_feed.py               # write podcast.xml from episodes.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from email.utils import formatdate
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
MANIFEST = DATA_DIR / "episodes.json"
SHOW_CONFIG = DATA_DIR / "podcast.json"
FEED_OUT = HERE / "podcast.xml"

SHOW_DEFAULTS = {
    "title": "My Podcast",
    "description": "Long-form podcasts, distilled into short NPR-style radio "
                   "segments for the drive.",
    "author": "Your Name",
    "email": "you@example.com",
    "language": "en-us",
    "category": "News",
    "explicit": False,
    "site_url": "https://YOUR-HOST.example",
    "base_url": "https://YOUR-HOST.example/episodes",
    "image_url": "https://YOUR-HOST.example/cover.jpg",
}


def init_config() -> None:
    if SHOW_CONFIG.exists():
        sys.exit(f"{SHOW_CONFIG.name} already exists; edit it directly.")
    SHOW_CONFIG.write_text(json.dumps(SHOW_DEFAULTS, indent=2), encoding="utf-8")
    print(f"Wrote {SHOW_CONFIG.name}. Edit base_url / site_url / image_url to your host, "
          "then re-run without --init.")


def mp3_duration(path: Path) -> str | None:
    try:
        from mutagen.mp3 import MP3
        secs = int(MP3(str(path)).info.length)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
    except Exception:
        return None


def rfc822(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=8, tzinfo=timezone.utc)
        return formatdate(d.timestamp())
    except ValueError:
        return formatdate()


def build_item(ep: dict, show: dict) -> str:
    title = escape(ep.get("title", "Episode"))
    guid = escape(ep.get("video_id", title))
    pub = rfc822(ep.get("date", ""))
    base = show["base_url"].rstrip("/")
    mp3_name = Path(ep["mp3_file"]).name
    mp3_url = f"{base}/{mp3_name}"

    mp3_path = HERE / ep["mp3_file"]
    length = mp3_path.stat().st_size if mp3_path.exists() else 0
    if not mp3_path.exists():
        print(f"  WARNING: {ep['mp3_file']} not found; enclosure length=0", file=sys.stderr)

    # Episode notes: the script text plus attribution to the source video.
    raw_summary = ""
    script_path = HERE / ep.get("script_file", "")
    if script_path.exists():
        raw_summary = script_path.read_text(encoding="utf-8").strip()
    raw_summary = raw_summary or ep.get("title", "Episode")

    url = ep.get("source_url", "")
    channel = ep.get("channel", "")
    by = f" by {channel}" if channel else ""
    attribution = (f'This is an AI-generated summary. Originally from "{ep.get("title", "")}"'
                   f"{by} on YouTube: {url}" if url else "")
    notes = escape(f"{raw_summary}\n\n{attribution}".strip())
    link_tag = f"\n      <link>{escape(url)}</link>" if url else ""

    dur = mp3_duration(mp3_path) if mp3_path.exists() else None
    dur_tag = f"\n      <itunes:duration>{dur}</itunes:duration>" if dur else ""

    return f"""    <item>
      <title>{title}</title>{link_tag}
      <description>{notes}</description>
      <itunes:summary>{notes}</itunes:summary>
      <pubDate>{pub}</pubDate>
      <guid isPermaLink="false">{guid}</guid>
      <enclosure url="{escape(mp3_url)}" length="{length}" type="audio/mpeg"/>
      <itunes:explicit>{'yes' if show.get('explicit') else 'no'}</itunes:explicit>{dur_tag}
    </item>"""


def build_feed() -> None:
    if not SHOW_CONFIG.exists():
        sys.exit("No podcast.json — run:  python3 build_feed.py --init")
    show = {**SHOW_DEFAULTS, **json.loads(SHOW_CONFIG.read_text(encoding="utf-8"))}
    episodes = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    if not episodes:
        print("No episodes in episodes.json yet — writing an empty feed.", file=sys.stderr)

    # Newest first.
    episodes = sorted(episodes, key=lambda e: e.get("date", ""), reverse=True)
    items = "\n".join(build_item(e, show) for e in episodes)

    title = escape(show["title"])
    desc = escape(show["description"])
    author = escape(show["author"])
    email = escape(show["email"])
    site = escape(show["site_url"])
    image = escape(show["image_url"])
    category = escape(show["category"])
    explicit = "yes" if show.get("explicit") else "no"
    now = formatdate(datetime.now(timezone.utc).timestamp())

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{title}</title>
    <link>{site}</link>
    <language>{escape(show['language'])}</language>
    <description>{desc}</description>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{author}</itunes:author>
    <itunes:summary>{desc}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>{explicit}</itunes:explicit>
    <itunes:image href="{image}"/>
    <itunes:category text="{category}"/>
    <itunes:owner>
      <itunes:name>{author}</itunes:name>
      <itunes:email>{email}</itunes:email>
    </itunes:owner>
{items}
  </channel>
</rss>
"""
    FEED_OUT.write_text(feed, encoding="utf-8")
    print(f"Wrote {FEED_OUT.name} with {len(episodes)} episode(s).")


def main() -> None:
    p = argparse.ArgumentParser(description="Build a podcast RSS feed from episodes.json")
    p.add_argument("--init", action="store_true", help="Scaffold podcast.json and exit")
    args = p.parse_args()
    if args.init:
        init_config()
    else:
        build_feed()


if __name__ == "__main__":
    main()
