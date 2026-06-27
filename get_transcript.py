#!/usr/bin/env python3
"""
Capture a YouTube video's transcript + metadata, for the podcast pipeline.

IMPORTANT: run this on your own machine (open network access). It does NOT
work inside the Cowork sandbox, whose network blocks YouTube.

Outputs two files next to each other:
    <id>.txt    the transcript text (fed to the summarizer)
    <id>.json   {video_id, title, description, source} metadata

Fallback chain:
    1. youtube-transcript-api   (fast, free, uses existing captions)
    2. yt-dlp                    (downloads the caption track if #1 fails)
    3. (optional) Whisper        (only if there are no captions at all)

Setup:
    pip install youtube-transcript-api yt-dlp
    # optional, only for caption-less videos:  pip install faster-whisper

Usage:
    python3 get_transcript.py "https://www.youtube.com/watch?v=32u5T6lO8qk"
    python3 get_transcript.py 32u5T6lO8qk --out-dir transcripts --lang en
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_video_id(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    sys.exit(f"Could not parse a video ID from: {s}")


# ----------------------- transcript sources -----------------------

def via_transcript_api(video_id: str, lang: str) -> str | None:
    """Works across old and new youtube-transcript-api versions."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    # New API (instance .fetch)
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=[lang, "en"])
        return " ".join(snip.text for snip in fetched).strip()
    except Exception:
        pass
    # Old API (classmethod .get_transcript)
    try:
        data = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang, "en"])
        return " ".join(d["text"] for d in data).strip()
    except Exception:
        return None


def _parse_vtt(vtt: str) -> str:
    lines, seen = [], set()
    for raw in vtt.splitlines():
        ln = raw.strip()
        if (not ln or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:"))
                or ln.isdigit()):
            continue
        ln = re.sub(r"<[^>]+>", "", ln)  # strip inline timing tags
        if ln and ln not in seen:
            seen.add(ln)
            lines.append(ln)
    return " ".join(lines).strip()


def via_ytdlp(video_id: str, lang: str) -> str | None:
    """Download the caption track with yt-dlp and flatten it to text."""
    if not _have("yt-dlp"):
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-lang", f"{lang}.*,en.*", "--sub-format", "vtt",
            "-o", str(Path(tmp) / "%(id)s.%(ext)s"), url,
        ]
        if subprocess.run(cmd, capture_output=True, text=True).returncode != 0:
            return None
        vtts = list(Path(tmp).glob("*.vtt"))
        if not vtts:
            return None
        return _parse_vtt(vtts[0].read_text(encoding="utf-8", errors="ignore"))


def via_whisper(video_id: str) -> str | None:
    """Last resort: download audio and transcribe locally. Slow."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    if not _have("yt-dlp"):
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "a.m4a"
        cmd = ["yt-dlp", "-f", "bestaudio", "-o", str(audio), url]
        if subprocess.run(cmd, capture_output=True, text=True).returncode != 0:
            return None
        model = WhisperModel("base.en", compute_type="int8")
        segments, _ = model.transcribe(str(audio))
        return " ".join(seg.text.strip() for seg in segments).strip()


# ----------------------- metadata -----------------------

def fetch_metadata(video_id: str) -> dict:
    meta = {"video_id": video_id, "title": "", "description": "", "channel": ""}
    if _have("yt-dlp"):
        url = f"https://www.youtube.com/watch?v={video_id}"
        r = subprocess.run(["yt-dlp", "--skip-download", "--dump-json", url],
                           capture_output=True, text=True)
        if r.returncode == 0:
            try:
                j = json.loads(r.stdout)
                meta["title"] = j.get("title", "")
                meta["description"] = j.get("description", "")
                meta["channel"] = j.get("channel") or j.get("uploader", "")
            except json.JSONDecodeError:
                pass
    # Fallback: YouTube oEmbed gives a reliable title + channel, no API key needed.
    if not meta["title"] or not meta["channel"]:
        try:
            import urllib.parse
            import urllib.request
            watch = f"https://www.youtube.com/watch?v={video_id}"
            oembed = ("https://www.youtube.com/oembed?url="
                      + urllib.parse.quote(watch, safe="") + "&format=json")
            with urllib.request.urlopen(oembed, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            meta["title"] = meta["title"] or data.get("title", "")
            meta["channel"] = meta["channel"] or data.get("author_name", "")
        except Exception:
            pass
    return meta


def _have(exe: str) -> bool:
    from shutil import which
    return which(exe) is not None


# ----------------------- reusable capture -----------------------

def capture(video: str, lang: str = "en", allow_whisper: bool = False) -> dict:
    """Capture transcript + metadata. Returns a dict the runner consumes.

    Keys: video_id, title, description, transcript (may be None), source.
    """
    vid = parse_video_id(video)
    text, source = None, None
    for name, fn in (("youtube-transcript-api", lambda: via_transcript_api(vid, lang)),
                     ("yt-dlp", lambda: via_ytdlp(vid, lang))):
        text = fn()
        if text:
            source = name
            break
    if not text and allow_whisper:
        text = via_whisper(vid)
        source = "whisper" if text else None

    meta = fetch_metadata(vid)
    meta["transcript"] = text
    meta["source"] = source
    return meta


# ----------------------- main -----------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Capture a YouTube transcript + metadata")
    p.add_argument("video", help="YouTube URL or 11-char video ID")
    p.add_argument("--out-dir", default=".", help="Output folder (default: current dir)")
    p.add_argument("--lang", default="en", help="Preferred caption language (default: en)")
    p.add_argument("--whisper", action="store_true",
                   help="Allow Whisper fallback for caption-less videos")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = capture(args.video, lang=args.lang, allow_whisper=args.whisper)
    vid = meta["video_id"]
    text = meta.pop("transcript")

    if not text:
        sys.exit("ERROR: could not capture a transcript. No captions found; "
                 "re-run with --whisper to transcribe the audio, or check the video ID.")

    txt_path = out_dir / f"{vid}.txt"
    json_path = out_dir / f"{vid}.json"
    txt_path.write_text(text, encoding="utf-8")
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    words = len(text.split())
    print(f"Transcript via {meta['source']}: {words} words -> {txt_path}")
    print(f"Metadata -> {json_path}  (title: {meta['title'] or 'n/a'})")


if __name__ == "__main__":
    main()
