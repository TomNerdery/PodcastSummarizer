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
import shutil
import subprocess
import sys
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

# Short silences between segments, in seconds.
#
# These were crossfades, which was a bug: acrossfade overlaps the intro's tail
# with the FIRST seconds of the narration and ramps the voice up from silence,
# so the opening words ("From the driver's seat...") faded in almost inaudibly.
# The stings already carry their own fades from make_stings.py, so a clean join
# with a breath either side sounds right and never touches the voice's level.
INTRO_GAP = 0.25
OUTRO_GAP = 0.35
SILENCE = "anullsrc=channel_layout=stereo:sample_rate=44100"

AFORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


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


def build_episode(narration: Path, out_path: Path,
                  intro: Path | None = None, outro: Path | None = None) -> None:
    """Wrap `narration` with the stings and write `out_path`.

    With no explicit paths, rotates to the next tune. Falls back to copying the
    narration verbatim on any problem.
    """
    if intro is None and outro is None:
        intro, outro = next_pair()
    use_intro = bool(intro and intro.exists())
    use_outro = bool(outro and outro.exists())

    if not which("ffmpeg"):
        print("  assemble: ffmpeg not found; using bare narration")
        shutil.copyfile(narration, out_path)
        return
    if not (use_intro or use_outro):
        print(f"  assemble: no stings in {ASSETS_DIR}; using bare narration")
        shutil.copyfile(narration, out_path)
        return

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    chain: list[tuple[str, int]] = []
    idx = 0
    if use_intro:
        args += ["-i", str(intro)]
        chain.append(("intro", idx))
        idx += 1
    args += ["-i", str(narration)]
    chain.append(("voice", idx))
    idx += 1
    if use_outro:
        args += ["-i", str(outro)]
        chain.append(("outro", idx))
        idx += 1

    # Normalise every piece to its target loudness first, so the segments join
    # at a matched level.
    filters = [
        f"[{i}:a]loudnorm=I={NARRATION_LUFS if name == 'voice' else MUSIC_LUFS}"
        f":TP=-1.5:LRA=11,{AFORMAT}[{name}]"
        for name, i in chain
    ]

    # Join end to end with a short silence between, never overlapping. Any
    # overlap has to fade one side down, and the side that loses is always the
    # voice's first or last words.
    order = [chain[0][0]]
    for n in range(1, len(chain)):
        gap = INTRO_GAP if chain[n - 1][0] == "intro" else OUTRO_GAP
        label = f"gap{n}"
        args += ["-f", "lavfi", "-t", f"{gap:.2f}", "-i", SILENCE]
        filters.append(f"[{idx}:a]{AFORMAT}[{label}]")
        idx += 1
        order += [label, chain[n][0]]

    joined = "".join(f"[{lbl}]" for lbl in order)
    filters.append(f"{joined}concat=n={len(order)}:v=0:a=1[out]")

    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-codec:a", "libmp3lame", "-b:a", "128k", str(out_path)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        print(f"  assemble: ffmpeg failed ({result.returncode}); using bare narration",
              file=sys.stderr)
        print(f"  {result.stderr.strip()[:300]}", file=sys.stderr)
        shutil.copyfile(narration, out_path)
        return

    tune = intro.stem if use_intro else (outro.stem if use_outro else "none")
    parts = "+".join(name for name, _ in chain)
    print(f"  assemble: {parts} [{tune}] -> {out_path.name} "
          f"({out_path.stat().st_size / 1024:.0f} KB)")


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
