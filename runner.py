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
import shutil
import sys
import tempfile
from pathlib import Path

import requests

import clips
from assemble import NARRATION_SUFFIX, assemble_parts, build_episode
from get_transcript import capture, parse_video_id
from summarize import generate_script
from tts_elevenlabs import (
    DEFAULT_MODEL, DEFAULT_VOICE_ID, QuotaExceeded, build_candidates, choose_voice,
    get_anthropic_key, get_api_key, load_config, load_dotenv, load_recent,
    remember_voice, reporter_for, smart_match, synthesize,
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
    """Cast a narrator, steering away from the ones used most recently.

    Without the `avoid` list every mode converges on the same one or two voices:
    'smart' because the best-fitting voice for a news read is the same one every
    day, 'random' because chance repeats. The history lives on DATA_DIR so it
    survives across runs.
    """
    default = config.get("default_voice") or DEFAULT_VOICE_ID
    avoid = load_recent()
    if mode == "smart":
        candidates = build_candidates(config, el_key)
        voice = smart_match(script, candidates, get_anthropic_key(), default, avoid=avoid)
    else:
        voice = choose_voice(mode, script, "", config, avoid=avoid)
    remember_voice(voice)
    return voice


# ----------------------- core -----------------------

def process_video(video: str, mode: str, lang: str, allow_whisper: bool) -> dict | None:
    vid = parse_video_id(video)
    print(f"\n=== {vid} ===")

    cap = capture(video, lang=lang, allow_whisper=allow_whisper)
    if not cap.get("transcript"):
        print(f"  SKIP: no transcript (try --whisper). source={cap.get('source')}")
        return None
    print(f"  transcript: {len(cap['transcript'].split())} words via {cap['source']}")

    # Timed segments are only offered to the summarizer when clips are switched
    # on, so the default path sends the same flat transcript it always has.
    segments = cap.get("segments") if clips.enabled() else None

    def write_script(with_clips: bool) -> str:
        return generate_script(cap["transcript"], cap.get("title", ""),
                               cap.get("description", ""),
                               segments=segments if with_clips else None)

    script = write_script(bool(segments))
    print(f"  script: {len(script.split())} words")

    el_key = get_api_key()
    config = load_config()
    voice = pick_voice(mode, script, config, el_key)

    # The script is written before the narrator is cast, so it carries a
    # {{REPORTER}} placeholder in the sign-off. Only now do we know who reads it.
    reporter = reporter_for(config, voice)
    script = script.replace("{{REPORTER}}", reporter)
    print(f"  reporter: {reporter}")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    date = dt.date.today().isoformat()
    base = f"{date}-{slugify(cap.get('title') or vid)}"
    script_path = EPISODES_DIR / f"{base}.txt"
    mp3_path = EPISODES_DIR / f"{base}.mp3"
    narration_path = EPISODES_DIR / f"{base}{NARRATION_SUFFIX}"

    # Cut the clips BEFORE paying for any narration. Fetching is free, so a
    # failure here costs nothing, and it must be settled first: the script's
    # hand-off lines ("here is Grantham describing...") only make sense if the
    # clip that follows actually exists. An all-or-nothing rule is what stops a
    # dangling hand-off reaching the feed.
    clip_files: list[Path] = []
    clip_spans: list[tuple[float, float]] = []
    chunks: list[str] = []
    if segments and clips.parse_script(script)[1]:
        nominated = clips.parse_script(script)[1]
        capped = clips.sanitize(nominated)
        if len(capped) == len(nominated):
            with tempfile.TemporaryDirectory() as tmp:
                made = clips.extract(vid, capped, Path(tmp))
                if len(made) == len(capped):
                    clip_files = [Path(shutil.copy(m, EPISODES_DIR / f"{base}.clip{i}.mp3"))
                                  for i, m in enumerate(made, start=1)]
                    clip_spans = capped
                    chunks = clips.parse_script(script)[0]
        if not clip_files:
            # Rewrite cleanly rather than ship a hand-off with nothing after it.
            # One extra Claude call, only on the failure path.
            print("  clips: could not place them all, rewriting without clips")
            script = write_script(False).replace("{{REPORTER}}", reporter)

    spoken = clips.strip_markers(script)
    script_path.write_text(spoken, encoding="utf-8")

    # Narrate, then wrap with the intro/outro stings so episodes do not run
    # straight into each other in the car.
    #
    # The narration master is KEPT, not thrown away in a temp dir. Assembly is
    # free to redo, TTS is not, so any later change to the stings or the join
    # can be re-applied without paying to re-voice. Learned the hard way: a
    # crossfade bug meant 35 published episodes could only be repaired by
    # re-voicing them. With clips the master is the finished body (narration and
    # clips interleaved, still no stings), which keeps that property intact.
    if clip_files:
        with tempfile.TemporaryDirectory() as tmp:
            body: list[tuple[Path, str]] = []
            for i, chunk in enumerate(chunks):
                if chunk.strip():
                    part = Path(tmp) / f"chunk{i}.mp3"
                    synthesize(el_key, chunk, part, voice, DEFAULT_MODEL)
                    body.append((part, "voice"))
                if i < len(clip_files):  # chunk, clip, chunk, clip, chunk
                    body.append((clip_files[i], "clip"))
            assemble_parts(body, narration_path)
        print(f"  body: {sum(1 for c in chunks if c.strip())} narration part(s) "
              f"+ {len(clip_files)} clip(s) [{clips.describe(clip_spans)}]")
    else:
        synthesize(el_key, spoken, narration_path, voice, DEFAULT_MODEL)
    tune = build_episode(narration_path, mp3_path)

    entry = {
        "video_id": vid,
        "title": cap.get("title", "") or vid,
        "channel": cap.get("channel", ""),
        "source_url": f"https://www.youtube.com/watch?v={vid}",
        "date": date,
        "voice": voice,
        "reporter": reporter,
        # Relative to DATA_DIR, which is where these actually live. Resolving
        # against HERE (the code dir) is what silently binned every episode
        # between the k3s migration and August 7, 2026.
        "narration_file": str(narration_path.relative_to(DATA_DIR)),
        # Which tune this episode was published with. Recorded so a later
        # re-render can put it back on the same one: the rotation only knows what
        # comes next, so without this every re-rendered episode silently changes
        # its music, which is what happened to 16 of them on August 12, 2026.
        "sting": tune,
        "clips": [{"start": s, "end": e} for s, e in clip_spans],
        "clip_files": [str(c.relative_to(DATA_DIR)) for c in clip_files],
        "script_file": str(script_path.relative_to(DATA_DIR)),
        "mp3_file": str(mp3_path.relative_to(DATA_DIR)),
        "summary_words": len(spoken.split()),
    }
    # The audio is paid for the instant synthesize() returns, so bank it in the
    # manifest right here. Nothing downstream may discard it.
    record_episode(entry)
    return entry


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
        if entry:  # process_video already recorded it
            print(f"\nDone -> {entry['mp3_file']}")
        return

    # playlist mode
    processed = set(load_json(PROCESSED, {"ids": []}).get("ids", []))
    all_ids = playlist_video_ids(args.playlist)
    todo = all_ids if args.reprocess else [v for v in all_ids if v not in processed]
    print(f"Playlist has {len(all_ids)} videos; {len(todo)} to process.")

    def mark_done(vid: str) -> None:
        processed.add(vid)
        save_json(PROCESSED, {"ids": sorted(processed)})

    done, stopped_early = 0, False
    for vid in todo:
        try:
            entry = process_video(vid, args.voice_mode, args.lang, args.whisper)
        except QuotaExceeded as e:
            # Every remaining video would fail the same way and the credits are
            # gone regardless. Stop cleanly, stay unprocessed, retry next run.
            print(f"\n  STOPPING: {e}")
            stopped_early = True
            break
        except Exception as e:  # keep going through the playlist
            print(f"  ERROR on {vid}: {e}")
            # Mark it done anyway. Anything that got this far may already have
            # spent ElevenLabs credits, and a blind retry pays for it twice.
            mark_done(vid)
            continue
        if entry:
            done += 1
        mark_done(vid)

    print(f"\nProcessed {done} new episode(s). Manifest: {MANIFEST.name}")
    if stopped_early:
        print(f"{len(todo) - done} video(s) left queued for the next run.")
    # Exit 0 either way: build_feed and publish must still run so whatever was
    # produced actually reaches the feed.


if __name__ == "__main__":
    main()
