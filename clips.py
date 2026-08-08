#!/usr/bin/env python3
"""
Cut short clips of the original audio out of a YouTube source.

Turns "[[CLIP 412.5-424.0]]" markers, which the summarizer places in the script,
into real audio: fetch just that slice with yt-dlp, snap the cut to nearby
silence so it never starts mid-word, and trim it exactly.

Disabled unless CLIPS_ENABLED=1. Off by default on purpose: embedding real
copyrighted audio is a different rights posture from publishing an AI
paraphrase, and whoever runs this should make that call deliberately rather than
inherit it. See the caps below, which are enforced, not advisory.

Every failure is soft. If anything here does not work, the episode is published
as narration only rather than not at all.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

# Enforced limits. Short excerpts used to illustrate an argument sit far better
# under fair use than long ones or the "best bit" of the source.
MAX_CLIP_SECONDS = 15.0
MIN_CLIP_SECONDS = 4.0
MAX_TOTAL_SECONDS = 45.0
MAX_CLIPS = 3

# How far either side of the nominated boundary to look for a natural pause.
SNAP_WINDOW = 1.5
SNAP_NOISE_DB = -32
SNAP_MIN_SILENCE = 0.18

# Extra audio fetched around the clip so there is room to snap outward.
FETCH_PAD = 4.0

CLIP_RE = re.compile(r"^\s*\[\[\s*CLIP\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\]\]\s*$",
                     re.MULTILINE | re.IGNORECASE)


def enabled() -> bool:
    return os.environ.get("CLIPS_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def available() -> tuple[bool, str]:
    if not enabled():
        return False, "CLIPS_ENABLED is not set"
    if not which("yt-dlp"):
        return False, "yt-dlp not installed"
    if not which("ffmpeg"):
        return False, "ffmpeg not installed"
    return True, ""


# ----------------------------- markers -----------------------------

def parse_script(script: str) -> tuple[list[str], list[tuple[float, float]]]:
    """Split a script on its clip markers.

    Returns (narration_chunks, clips). There is always exactly one more chunk
    than clip, so the pieces interleave chunk, clip, chunk, clip, chunk.
    """
    clips: list[tuple[float, float]] = []
    chunks: list[str] = []
    last = 0
    for m in CLIP_RE.finditer(script):
        start, end = float(m.group(1)), float(m.group(2))
        chunks.append(script[last:m.start()].strip())
        clips.append((start, end))
        last = m.end()
    chunks.append(script[last:].strip())
    return chunks, clips


def strip_markers(script: str) -> str:
    """The script as it should be read aloud, with markers removed."""
    return re.sub(r"\n{3,}", "\n\n", CLIP_RE.sub("", script)).strip()


def sanitize(clips: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Apply the caps. Anything that cannot be made to fit is dropped."""
    out: list[tuple[float, float]] = []
    total = 0.0
    for start, end in clips:
        if end <= start:
            continue
        length = min(end - start, MAX_CLIP_SECONDS)
        if length < MIN_CLIP_SECONDS:
            continue
        if len(out) >= MAX_CLIPS or total + length > MAX_TOTAL_SECONDS:
            break
        out.append((start, start + length))
        total += length
    return out


# ----------------------------- audio -----------------------------

def _run(args: list[str], timeout: int = 300):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def fetch_section(video_id: str, start: float, end: float, dest: Path) -> bool:
    """Download only the needed slice, padded, rather than the whole episode."""
    lo = max(start - FETCH_PAD, 0.0)
    hi = end + FETCH_PAD
    url = f"https://www.youtube.com/watch?v={video_id}"
    r = _run([
        "yt-dlp", "-f", "bestaudio", "--quiet", "--no-warnings",
        "--download-sections", f"*{lo:.2f}-{hi:.2f}",
        "--force-keyframes-at-cuts",
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "-o", str(dest.with_suffix("")) + ".%(ext)s", url,
    ], timeout=420)
    if r.returncode != 0:
        print(f"    clip fetch failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return dest.exists()


def silence_points(path: Path) -> list[float]:
    """Times where a silence begins or ends, for snapping cuts to."""
    r = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
              f"silencedetect=noise={SNAP_NOISE_DB}dB:d={SNAP_MIN_SILENCE}",
              "-f", "null", "-"], timeout=180)
    points = [float(m) for m in re.findall(r"silence_(?:start|end):\s*(-?[\d.]+)", r.stderr)]
    return sorted(p for p in points if p >= 0)


def snap(target: float, points: list[float], window: float = SNAP_WINDOW) -> float:
    """Nearest natural pause, if one is close enough. Caption timings drift from
    the waveform by a second or so, which is the difference between a clean
    quote and one that starts mid-word."""
    near = [p for p in points if abs(p - target) <= window]
    return min(near, key=lambda p: abs(p - target)) if near else target


def cut(src: Path, dest: Path, start: float, end: float) -> bool:
    """Trim to exactly the wanted span, with tiny fades so it cannot click."""
    length = max(end - start, 0.1)
    fade = min(0.08, length / 4)
    r = _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
              "-af", f"afade=t=in:st=0:d={fade:.3f},"
                     f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}",
              "-codec:a", "libmp3lame", "-b:a", "128k", str(dest)], timeout=180)
    return r.returncode == 0 and dest.exists()


def extract(video_id: str, clips: list[tuple[float, float]], work_dir: Path) -> list[Path]:
    """Fetch and cut every clip. Returns only the ones that worked."""
    ok, why = available()
    if not ok:
        if enabled():
            print(f"  clips: skipped ({why})")
        return []

    clips = sanitize(clips)
    if not clips:
        return []

    work_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    used = 0.0
    for n, (start, end) in enumerate(clips, start=1):
        raw = work_dir / f"clip{n}-raw.mp3"
        out = work_dir / f"clip{n}.mp3"
        if not fetch_section(video_id, start, end, raw):
            continue

        # Times inside the padded download, not the original video.
        offset = max(start - FETCH_PAD, 0.0)
        local_start, local_end = start - offset, end - offset
        points = silence_points(raw)
        snapped_start = snap(local_start, points)
        snapped_end = snap(local_end, points)
        if snapped_end - snapped_start < MIN_CLIP_SECONDS:
            snapped_start, snapped_end = local_start, local_end  # snap made it useless

        # Snapping moves a boundary to the nearest pause, which can be OUTWARD,
        # so a span that fitted the caps before snapping can exceed them after.
        # Re-clamp here or the limits are advisory rather than enforced.
        snapped_end = min(snapped_end, snapped_start + MAX_CLIP_SECONDS,
                          snapped_start + max(MAX_TOTAL_SECONDS - used, 0.0))
        length = snapped_end - snapped_start
        if length < MIN_CLIP_SECONDS:
            print(f"  clip {n}: dropped, no room left under the {MAX_TOTAL_SECONDS:.0f}s cap")
            raw.unlink(missing_ok=True)
            continue

        if cut(raw, out, snapped_start, snapped_end):
            made.append(out)
            used += length
            drift = (snapped_start - local_start, snapped_end - local_end)
            print(f"  clip {n}: {start:.1f}s-{end:.1f}s -> {length:.1f}s "
                  f"(snapped {drift[0]:+.2f}s/{drift[1]:+.2f}s)")
        raw.unlink(missing_ok=True)
    return made


def describe(clips: list[tuple[float, float]]) -> str:
    """Human-readable timestamps, for the episode's show notes."""
    def hhmmss(t: float) -> str:
        t = int(t)
        return f"{t // 3600}:{(t % 3600) // 60:02d}:{t % 60:02d}" if t >= 3600 \
            else f"{(t % 3600) // 60}:{t % 60:02d}"
    return ", ".join(f"{hhmmss(s)}-{hhmmss(e)}" for s, e in clips)
