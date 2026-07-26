#!/usr/bin/env python3
"""
Podcast Summaries runner — one command turns a YouTube video (or a whole
playlist) into a published-ready radio episode.

Per video it chains:  capture transcript -> Claude summary -> pick voice -> ElevenLabs MP3
and records the result in episodes.json (the manifest the RSS/publish step will read).

IMPORTANT: run this on your own machine (open network access). It needs to
reach YouTube, which the Cowork sandbox cannot.

Setup:
    pip install requests youtube-transcript-api yt-dlp
    # in .env (next to this file):
    #   ELEVENLABS_API_KEY=...
    #   ANTHROPIC_API_KEY=...
    #   YOUTUBE_API_KEY=...        (only needed for --playlist)

Usage:
    python3 runner.py "https://www.youtube.com/watch?v=32u5T6lO8qk"
    python3 runner.py --playlist PLxxxxxxxx          # process new items only
    python3 runner.py <id> --voice-mode rotate       # override voice selection
    python3 runner.py --playlist PLxxxx --reprocess   # ignore the processed list
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

from get_transcript import capture, parse_video_id
from summarize import generate_script
from tts_elevenlabs import (
    DEFAULT_MODEL, DEFAULT_VOICE_ID, build_candidates, choose_voice,
    get_anthropic_key, get_api_key, load_config, load_dotenv, smart_match, synthesize,
)

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
EPISODES_DIR = DATA_DIR / "episodes"
MANIFEST = DATA_DIR / "episodes.json"
PROCESSED = DATA_DIR / "processed.json"
YT_API = "https://www.googleapis.com/youtube/v3/playlistItems"


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:maxlen].strip("-") or "episode"


# ----------------------- state -----------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ----------------------- voice -----------------------

def pick_voice(mode: str, script: str, config: dict, el_key: str) -> str:
    default = config.get("default_voice") or DEFAULT_VOICE_ID
    if mode == "smart":
        candidates = build_candidates(config, el_key)
        return smart_match(script, candidates, get_anthropic_key(), default)
    return choose_voice(mode, script, "", config)


# ----------------------- core -----------------------

def process_video(video: str, mode: str, lang: str, allow_whisper: bool) -> dict | None:
    vid = parse_video_id(video)
    print(f"\n=== {vid} ===")

    cap = capture(video, lang=lang, allow_whisper=allow_whisper)
    if not cap.get("transcript"):
        print(f"  SKIP: no transcript (try --whisper). source={cap.get('source')}")
        return None
    print(f"  transcript: {len(cap['transcript'].split())} words via {cap['source']}")

    script = generate_script(cap["transcript"], cap.get("title", ""), cap.get("description", ""))
    print(f"  script: {len(script.split())} words")

    el_key = get_api_key()
    config = load_config()
    voice = pick_voice(mode, script, config, el_key)

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    date = dt.date.today().isoformat()
    base = f"{date}-{slugify(cap.get('title') or vid)}"
    script_path = EPISODES_DIR / f"{base}.txt"
    mp3_path = EPISODES_DIR / f"{base}.mp3"
    script_path.write_text(script, encoding="utf-8")
    synthesize(el_key, script, mp3_path, voice, DEFAULT_MODEL)

    return {
        "video_id": vid,
        "title": cap.get("title", "") or vid,
        "channel": cap.get("channel", ""),
        "source_url": f"https://www.youtube.com/watch?v={vid}",
        "date": date,
        "voice": voice,
        "script_file": str(script_path.relative_to(HERE)),
        "mp3_file": str(mp3_path.relative_to(HERE)),
        "summary_words": len(script.split()),
    }


def record_episode(entry: dict) -> None:
    episodes = load_json(MANIFEST, [])
    episodes = [e for e in episodes if e.get("video_id") != entry["video_id"]]
    episodes.append(entry)
    save_json(MANIFEST, episodes)


# ----------------------- playlist -----------------------

def playlist_video_ids(playlist_id: str) -> list:
    load_dotenv(HERE / ".env")
    import os
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("ERROR: YOUTUBE_API_KEY is not set (needed for --playlist).")
    ids, page = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id,
                  "maxResults": 50, "key": key}
        if page:
            params["pageToken"] = page
        r = requests.get(YT_API, params=params, timeout=30)
        if r.status_code != 200:
            sys.exit(f"YouTube API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids


# ----------------------- main -----------------------

def main() -> None:
    p = argparse.ArgumentParser(description="YouTube -> radio episode runner")
    p.add_argument("video", nargs="?", help="A YouTube URL or video ID (single-video mode)")
    p.add_argument("--playlist", help="A YouTube playlist ID (process new items)")
    p.add_argument("--voice-mode", default="smart",
                   choices=["smart", "match", "rotate", "random", "fixed"],
                   help="Voice selection (default: smart)")
    p.add_argument("--lang", default="en", help="Preferred caption language")
    p.add_argument("--whisper", action="store_true", help="Allow Whisper fallback")
    p.add_argument("--reprocess", action="store_true",
                   help="With --playlist, ignore the processed list and redo everything")
    args = p.parse_args()

    if not args.video and not args.playlist:
        p.error("provide a video URL/ID, or --playlist <ID>")

    if args.video:
        entry = process_video(args.video, args.voice_mode, args.lang, args.whisper)
        if entry:
            record_episode(entry)
            print(f"\nDone -> {entry['mp3_file']}")
        return

    # playlist mode
    processed = set(load_json(PROCESSED, {"ids": []}).get("ids", []))
    all_ids = playlist_video_ids(args.playlist)
    todo = all_ids if args.reprocess else [v for v in all_ids if v not in processed]
    print(f"Playlist has {len(all_ids)} videos; {len(todo)} to process.")

    done = 0
    for vid in todo:
        try:
            entry = process_video(vid, args.voice_mode, args.lang, args.whisper)
        except Exception as e:  # keep going through the playlist
            print(f"  ERROR on {vid}: {e}")
            continue
        if entry:
            record_episode(entry)
            done += 1
        processed.add(vid)
        save_json(PROCESSED, {"ids": sorted(processed)})

    print(f"\nProcessed {done} new episode(s). Manifest: {MANIFEST.name}")


if __name__ == "__main__":
    main()
