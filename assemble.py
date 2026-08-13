#!/usr/bin/env python3
"""
Assemble a finished episode:  intro sting -> narration -> outro sting.

Without this, episodes run straight into one another in the car and there is no
audio cue that a new topic has started. A short musical sting at each end fixes
that, and loudness-matching stops the music blasting over the narrator.

Assets are optional and live on the data volume, not in the image:

    $DATA_DIR/assets/intro.mp3
    $DATA_DIR/assets/outro.mp3

Drop in any files you like (see make_stings.py for a generated starter pair, or
use a CC0 track from Pixabay). If they are missing, or ffmpeg is not installed,
the bare narration is used instead. A missing sting must never cost an episode.

Usage as a script:
    python3 assemble.py narration.mp3 episode.mp3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
ASSETS_DIR = DATA_DIR / "assets"
INTRO = ASSETS_DIR / "intro.mp3"
OUTRO = ASSETS_DIR / "outro.mp3"
STING_STATE = DATA_DIR / ".sting_state.json"

# -16 LUFS is the podcast norm. The stings sit deliberately below the voice so
# the transition reads as a cue, not an interruption.
NARRATION_LUFS = -16.0
MUSIC_LUFS = -20.0

# Short silences between segments inside the body, in seconds.
INTRO_GAP = 0.25
OUTRO_GAP = 0.35
SILENCE = "anullsrc=channel_layout=stereo:sample_rate=44100"

# How long the music and the voice overlap at each sting join.
#
# The history matters here, because the obvious way to write this is the bug that
# 81bf788 removed. acrossfade overlapped the intro's tail with the FIRST seconds
# of the narration and ramped the VOICE up from silence, taking 6.8 dB off the
# opening line. That was replaced by a clean concat plus a breath, which never
# touched the voice but left a hard cut with a hole in it: measured on a published
# episode, the music reached -91 dB for a quarter of a second and the voice then
# arrived at -16.8 dB.
#
# The resolution is that only ONE side has to move. The music is faded across the
# overlap and the voice is mixed in flat: no fade, no attenuation, no delay
# relative to itself. A crossfade makes the voice lose; ducking the music does not.
INTRO_OVERLAP = 2.0
OUTRO_OVERLAP = 1.5

AFORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"

# The narration-only master is kept next to each episode. Assembly is cheap to
# redo; TTS is not. Keeping it means a later change to the stings or the join
# can be re-applied for free instead of paying to re-voice the back catalogue.
NARRATION_SUFFIX = ".narration.mp3"
CLIP_PATTERN = re.compile(r"\.clip\d+\.mp3$")


def is_intermediate(path: Path) -> bool:
    """Working audio that must never be published on its own.

    Narration masters are just noise in the bucket. Source clips are worse: a
    standalone excerpt of someone else's copyrighted audio, served as its own
    file, is a far weaker position than the same seconds embedded inside
    original commentary. They stay on the volume.
    """
    return path.name.endswith(NARRATION_SUFFIX) or bool(CLIP_PATTERN.search(path.name))


def episode_mp3s(directory: Path) -> list[Path]:
    """Finished episodes only, never the working audio sitting beside them."""
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.mp3") if not is_intermediate(p))


def sting_pairs() -> list[tuple[Path, Path]]:
    """Numbered intro/outro pairs, one per source track, in a stable order.

    A pair is always kept together: intro-3 goes with outro-3, both cut from the
    same tune. Opening on one song and closing on another sounds like a mistake.
    """
    pairs = []
    for intro in sorted(ASSETS_DIR.glob("intro-*.mp3")):
        outro = ASSETS_DIR / f"outro-{intro.stem.split('-', 1)[1]}.mp3"
        if outro.exists():
            pairs.append((intro, outro))
    return pairs


def pair_named(name: str) -> tuple[Path | None, Path | None]:
    """Resolve a recorded sting name ("intro-2") back to its pair.

    Lets a re-render put an episode back on the tune it was published with. The
    rotation alone cannot do that: it only knows what comes next, not what an
    episode had, which is why 16 episodes changed tune when the join was fixed on
    August 12, 2026. Anything unrecognised comes back as (None, None) so the
    caller falls through to rotating, rather than failing.
    """
    if not name:
        return (None, None)
    suffix = name.split("-", 1)[1] if "-" in name else ""
    intro = ASSETS_DIR / f"intro-{suffix}.mp3" if suffix else INTRO
    outro = ASSETS_DIR / f"outro-{suffix}.mp3" if suffix else OUTRO
    return (intro if intro.exists() else None, outro if outro.exists() else None)


def next_pair() -> tuple[Path | None, Path | None]:
    """Rotate to the next tune, so the show is not scored by one loop forever."""
    pairs = sting_pairs()
    if not pairs:
        return (INTRO if INTRO.exists() else None,
                OUTRO if OUTRO.exists() else None)
    try:
        idx = json.loads(STING_STATE.read_text()).get("next", 0)
    except (OSError, json.JSONDecodeError, AttributeError):
        idx = 0
    if not isinstance(idx, int) or idx < 0:
        idx = 0
    chosen = pairs[idx % len(pairs)]
    try:
        STING_STATE.write_text(json.dumps({"next": (idx + 1) % len(pairs)}, indent=2))
    except OSError as e:  # a read-only volume must not cost us an episode
        print(f"  (could not persist sting rotation: {e})", file=sys.stderr)
    return chosen


def have_stings() -> bool:
    return bool(sting_pairs()) or INTRO.exists() or OUTRO.exists()


# Loudness target per kind of segment. Clips match the narrator so a real voice
# from the source does not jump out against the AI one, which is the tell that
# gives away a bad edit.
LUFS = {"music": MUSIC_LUFS, "voice": NARRATION_LUFS, "clip": NARRATION_LUFS}

# Silence before a segment of each kind.
GAP_BEFORE = {"voice": INTRO_GAP, "music": OUTRO_GAP, "clip": 0.35}


def assemble_parts(parts: list[tuple[Path, str]], out_path: Path) -> bool:
    """Join (path, kind) segments end to end. Kind sets loudness and spacing."""
    parts = [(p, k) for p, k in parts if p and p.exists()]
    if not parts:
        return False

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters, labels = [], []
    idx = 0
    for n, (path, kind) in enumerate(parts):
        args += ["-i", str(path)]
        label = f"s{n}"
        filters.append(f"[{idx}:a]loudnorm=I={LUFS.get(kind, NARRATION_LUFS)}"
                       f":TP=-1.5:LRA=11,{AFORMAT}[{label}]")
        labels.append(label)
        idx += 1

    # Join end to end with a short silence between, never overlapping. This runs
    # over the body only, where every segment is somebody talking: overlapping a
    # clip with the narration around it would mean fading one voice under
    # another. The music joins are a different problem and live in mix_stings().
    order = [labels[0]]
    for n in range(1, len(parts)):
        gap = GAP_BEFORE.get(parts[n][1], 0.3)
        label = f"gap{n}"
        args += ["-f", "lavfi", "-t", f"{gap:.2f}", "-i", SILENCE]
        filters.append(f"[{idx}:a]{AFORMAT}[{label}]")
        idx += 1
        order += [label, labels[n]]

    joined = "".join(f"[{lbl}]" for lbl in order)
    filters.append(f"{joined}concat=n={len(order)}:v=0:a=1[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-codec:a", "libmp3lame", "-b:a", "128k", str(out_path)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        print(f"  assemble: ffmpeg failed ({result.returncode})", file=sys.stderr)
        print(f"  {result.stderr.strip()[:300]}", file=sys.stderr)
        return False
    return True


def duration(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def mix_stings(body: Path, intro: Path | None, outro: Path | None,
               out_path: Path) -> bool:
    """Lay the stings over the body, fading the MUSIC across each join.

    The body arrives already loudness-matched and is mixed in untouched: it is
    delayed to sit under the intro's tail, and nothing else is done to it. Every
    fade in this function applies to a music input only. See the note on
    INTRO_OVERLAP for why that distinction is the whole point.
    """
    body_len = duration(body)
    if body_len is None:
        return False

    intro_len = duration(intro) if intro else None
    if intro and intro_len is None:
        return False

    # The voice starts while the intro is still playing.
    body_at = max(intro_len - INTRO_OVERLAP, 0.0) if intro_len else 0.0

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters, labels, idx = [], [], 0

    if intro:
        args += ["-i", str(intro)]
        fade_at = max(intro_len - INTRO_OVERLAP, 0.0)
        fade_for = min(INTRO_OVERLAP, intro_len)
        filters.append(
            f"[{idx}:a]loudnorm=I={MUSIC_LUFS}:TP=-1.5:LRA=11,{AFORMAT},"
            f"afade=t=out:st={fade_at:.3f}:d={fade_for:.3f}[intro]")
        labels.append("intro")
        idx += 1

    args += ["-i", str(body)]
    delay_ms = int(round(body_at * 1000))
    filters.append(f"[{idx}:a]{AFORMAT}"
                   + (f",adelay={delay_ms}:all=1" if delay_ms else "") + "[body]")
    labels.append("body")
    idx += 1

    if outro:
        args += ["-i", str(outro)]
        # Comes up under the closing line and reaches full as the voice stops.
        starts_at = max(body_at + body_len - OUTRO_OVERLAP, 0.0)
        filters.append(
            f"[{idx}:a]loudnorm=I={MUSIC_LUFS}:TP=-1.5:LRA=11,{AFORMAT},"
            f"afade=t=in:st=0:d={OUTRO_OVERLAP:.3f},"
            f"adelay={int(round(starts_at * 1000))}:all=1[outro]")
        labels.append("outro")
        idx += 1

    # normalize=0 or amix divides by the input count and quietly drops the voice
    # 4.8 dB the moment a sting overlaps it. The limiter catches the summed peaks
    # instead, since both sides are loudnorm'd to TP=-1.5 and can add above 0 dBFS.
    joined = "".join(f"[{lbl}]" for lbl in labels)
    filters.append(f"{joined}amix=inputs={len(labels)}:duration=longest:normalize=0,"
                   f"alimiter=limit=0.95[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-codec:a", "libmp3lame", "-b:a", "128k", str(out_path)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        print(f"  assemble: sting mix failed ({result.returncode})", file=sys.stderr)
        print(f"  {result.stderr.strip()[:300]}", file=sys.stderr)
        return False
    return True


def build_episode(narration: Path, out_path: Path,
                  intro: Path | None = None, outro: Path | None = None,
                  body: list[tuple[Path, str]] | None = None) -> str | None:
    """Wrap the narration with the stings and write `out_path`.

    Returns the sting used ("intro-2"), or None if the episode ended up with no
    music at all. Callers should record it: without it a later re-render cannot
    put the episode back on the same tune, and the rotation will quietly change
    the music of everything it touches.

    `body` overrides the single narration file with an ordered list of
    (path, kind) segments, which is how narration/clip/narration episodes are
    built. With no explicit stings, rotates to the next tune. Falls back to the
    bare narration on any problem, because a missing sting must never cost an
    episode.
    """
    if intro is None and outro is None:
        intro, outro = next_pair()
    use_intro = bool(intro and intro.exists())
    use_outro = bool(outro and outro.exists())

    if not which("ffmpeg"):
        print("  assemble: ffmpeg not found; using bare narration")
        shutil.copyfile(narration, out_path)
        return None
    if not (use_intro or use_outro) and not body:
        print(f"  assemble: no stings in {ASSETS_DIR}; using bare narration")
        shutil.copyfile(narration, out_path)
        return None

    middle = body if body else [(narration, "voice")]

    def plain_join() -> bool:
        """The join that shipped before the overlap: correct, just abrupt."""
        parts: list[tuple[Path, str]] = []
        if use_intro:
            parts.append((intro, "music"))
        parts += middle
        if use_outro:
            parts.append((outro, "music"))
        return assemble_parts(parts, out_path)

    # Two passes: build the body, then lay the music over its ends. Doing it in
    # one filter graph would mean knowing every segment's duration up front, and
    # the second pass costs nothing that matters at this length.
    with tempfile.TemporaryDirectory() as tmp:
        body_path = Path(tmp) / "body.mp3"
        if not assemble_parts(middle, body_path):
            print("  assemble: falling back to bare narration", file=sys.stderr)
            shutil.copyfile(narration, out_path)
            return None

        if not mix_stings(body_path, intro if use_intro else None,
                          outro if use_outro else None, out_path):
            # The mix is the only new way this can fail. Losing the overlap is
            # much cheaper than losing the episode, so drop back a level first.
            if not plain_join():
                print("  assemble: falling back to bare narration", file=sys.stderr)
                shutil.copyfile(narration, out_path)
                return None
            print("  assemble: sting mix failed; used the plain join", file=sys.stderr)

    tune = intro.stem if use_intro else (outro.stem if use_outro else None)
    kinds = (["music"] if use_intro else []) + [k for _, k in middle] \
        + (["music"] if use_outro else [])
    print(f"  assemble: {'+'.join(kinds)} [{tune or 'none'}] -> {out_path.name} "
          f"({out_path.stat().st_size / 1024:.0f} KB)")
    return tune


def main() -> None:
    p = argparse.ArgumentParser(description="Wrap a narration MP3 with intro/outro stings")
    p.add_argument("narration", help="Narration MP3 from ElevenLabs")
    p.add_argument("output", help="Path for the finished episode MP3")
    p.add_argument("--intro", help=f"Override intro sting (default: {INTRO})")
    p.add_argument("--outro", help=f"Override outro sting (default: {OUTRO})")
    args = p.parse_args()

    build_episode(
        Path(args.narration), Path(args.output),
        Path(args.intro) if args.intro else None,
        Path(args.outro) if args.outro else None,
    )


if __name__ == "__main__":
    main()
