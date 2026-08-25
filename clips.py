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
# Last-resort reach, used ONLY when nothing at all was found inside SNAP_WINDOW.
# Deliberately a separate pass so a clip that already snaps cleanly is untouched.
SNAP_WINDOW_WIDE = 3.0
# A boundary that could not be snapped is a cut in the middle of a word. The
# normal 80ms fade makes that a chop; this makes it trail off like an edit.
UNSNAPPED_FADE = 0.25
SNAP_NOISE_DB = -32
SNAP_MIN_SILENCE = 0.18

# Which kind of silence boundary each end of a clip should land on. A clip opens
# where a silence ENDS, because that is where speech resumes, and closes where a
# silence STARTS, because that is where speech stops.
START_WANTS = "end"
END_WANTS = "start"

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
    ], timeout=180)  # a ~20s slice takes seconds; a long wait means it is stuck
    if r.returncode != 0:
        print(f"    clip fetch failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return dest.exists()


def silence_points(path: Path) -> list[tuple[str, float]]:
    """Times where a silence begins or ends, tagged with which it is.

    The kind matters and this used to throw it away. A clip should START where a
    silence ENDS (speech resuming) and END where a silence STARTS (speech
    stopping). Treating the two as interchangeable is a coin flip, and the wrong
    side of it puts the cut on a word onset rather than in the gap.
    """
    r = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
              f"silencedetect=noise={SNAP_NOISE_DB}dB:d={SNAP_MIN_SILENCE}",
              "-f", "null", "-"], timeout=180)
    found = re.findall(r"silence_(start|end):\s*(-?[\d.]+)", r.stderr)
    return sorted(((k, float(v)) for k, v in found if float(v) >= 0), key=lambda p: p[1])


def snap(target: float, points: list[tuple[str, float]], want: str,
         window: float = SNAP_WINDOW) -> tuple[float, bool]:
    """Nearest natural pause of the right kind. Returns (time, snapped?).

    Caption timings drift from the waveform by a second or so, which is the
    difference between a clean quote and one that starts mid-word. `want` is the
    boundary kind that suits this end of the clip; a slightly more distant
    boundary of the right kind beats a nearer one of the wrong kind, because the
    kind is what decides whether the cut lands in the gap or on a word.

    IT NOW SAYS WHETHER IT SUCCEEDED, and that is the actual fix. It used to
    return the untouched target when no pause was in range, so the caller
    computed a drift of exactly +0.00s and printed it in the same format as a
    real measurement. A cut that never snapped was indistinguishable in the log
    from a perfect one. That is the same trap the August 12 fix was written for
    ("a derived number in the same shape as a measured one"), left alive in the
    path that fix did not cover.

    Three passes, in this order, and the order is chosen so that ANY clip which
    already snapped keeps exactly the boundary it had:

      1. right kind, inside the normal window
      2. any kind, inside the normal window        <- 1 and 2 are the old behaviour
      3. right kind, inside the wider window       <- new, only reached on a miss

    Reaching further for the WRONG kind is deliberately not a pass. A distant
    boundary of the wrong kind lands on a word onset, which is what the typing
    work established as worse than not moving at all.
    """
    def best(candidates):
        return min(candidates, key=lambda p: abs(p[1] - target))[1]

    near = [p for p in points if abs(p[1] - target) <= window]
    right = [p for p in near if p[0] == want]
    if right:
        return best(right), True
    if near:
        return best(near), True

    wide = [p for p in points
            if p[0] == want and abs(p[1] - target) <= SNAP_WINDOW_WIDE]
    if wide:
        return best(wide), True

    return target, False


def snap_back(limit: float, points: list[tuple[str, float]], floor: float,
              want: str) -> float | None:
    """Last pause at or before `limit`, for when a cap forces the end inward.

    Only ever looks backward, so a caller using this to enforce a limit still
    enforces it. Prefers the wanted kind, same reasoning as `snap`. Returns None
    when there is no usable pause in range, which the caller should treat as
    "cut at the limit and say so" rather than pretend the boundary was snapped.
    """
    usable = [p for p in points if floor <= p[1] <= limit]
    if not usable:
        return None
    preferred = [p for p in usable if p[0] == want]
    return max(preferred or usable, key=lambda p: p[1])[1]


def cut(src: Path, dest: Path, start: float, end: float,
        out_fade: float | None = None) -> bool:
    """Trim to exactly the wanted span, with fades so it cannot click.

    `out_fade` lengthens only the trailing fade. Used when the end could not be
    snapped to a pause: the cut is mid-word whatever happens, and a longer fade
    makes it read as an edit rather than as the audio being chopped off.
    """
    length = max(end - start, 0.1)
    fade = min(0.08, length / 4)
    out = min(out_fade or fade, length / 3)
    r = _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
              "-af", f"afade=t=in:st=0:d={fade:.3f},"
                     f"afade=t=out:st={length - out:.3f}:d={out:.3f}",
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
        snapped_start, start_ok = snap(local_start, points, want=START_WANTS)
        snapped_end, end_ok = snap(local_end, points, want=END_WANTS)
        if snapped_end - snapped_start < MIN_CLIP_SECONDS:
            # Snapping collapsed the span. Fall back to the caption times, and
            # record that NEITHER boundary is on a pause any more, which the old
            # code did silently.
            snapped_start, snapped_end = local_start, local_end
            start_ok = end_ok = False

        # Snapping moves a boundary to the nearest pause, which can be OUTWARD,
        # so a span that fitted the caps before snapping can exceed them after.
        # Re-clamp here or the limits are advisory rather than enforced.
        limit = min(snapped_start + MAX_CLIP_SECONDS,
                    snapped_start + max(MAX_TOTAL_SECONDS - used, 0.0))
        capped = snapped_end > limit
        found_pause = True
        if capped:
            # The clamp used to assign `limit` straight to the end, which threw
            # the snap away and put the cut back in the middle of a word: a 15.0s
            # span snapped outward to 16.59s came back as exactly start+15.0,
            # landing 0.46s into a spoken word. Pull back to the last pause at or
            # before the limit instead. Backward only, so the caps stay enforced.
            pause = snap_back(limit, points, floor=snapped_start + MIN_CLIP_SECONDS,
                              want=END_WANTS)
            found_pause = pause is not None
            snapped_end = pause if found_pause else limit
            end_ok = found_pause
        length = snapped_end - snapped_start
        if length < MIN_CLIP_SECONDS:
            print(f"  clip {n}: dropped, no room left under the {MAX_TOTAL_SECONDS:.0f}s cap")
            raw.unlink(missing_ok=True)
            continue

        # A mid-word end is sometimes unavoidable. Make it trail off instead of
        # stopping dead, which is what "imprecise end cuts" actually sounded like.
        if cut(raw, out, snapped_start, snapped_end,
               out_fade=None if end_ok else UNSNAPPED_FADE):
            made.append(out)
            used += length
            # Say plainly whether the end was snapped or forced. The old line
            # printed one "snapped" figure per side, so a clamped end echoed the
            # start's drift and read as a clean snap when it was a raw cut.
            # Never print a drift figure for a boundary that was not snapped.
            # "+0.00s" for an untouched target is the exact tell that hid this
            # bug: it reads as a perfect snap and means the opposite.
            def drift(moved: float, orig: float, ok: bool) -> str:
                return f"{moved - orig:+.2f}s" if ok else "UNSNAPPED"
            note = ""
            if capped:
                note = (", capped to a pause" if found_pause
                        else ", capped mid-audio (no pause in range)")
            elif not end_ok:
                note = f", no pause within {SNAP_WINDOW_WIDE:.1f}s, {UNSNAPPED_FADE*1000:.0f}ms fade"
            print(f"  clip {n}: {start:.1f}s-{end:.1f}s -> {length:.1f}s "
                  f"(start {drift(snapped_start, local_start, start_ok)}, "
                  f"end {drift(snapped_end, local_end, end_ok)}{note})")
        raw.unlink(missing_ok=True)
    return made


def describe(clips: list[tuple[float, float]]) -> str:
    """Human-readable timestamps, for the episode's show notes."""
    def hhmmss(t: float) -> str:
        t = int(t)
        return f"{t // 3600}:{(t % 3600) // 60:02d}:{t % 60:02d}" if t >= 3600 \
            else f"{(t % 3600) // 60}:{t % 60:02d}"
    return ", ".join(f"{hhmmss(s)}-{hhmmss(e)}" for s, e in clips)
