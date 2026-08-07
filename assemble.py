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

# -16 LUFS is the podcast norm. The stings sit deliberately below the voice so
# the transition reads as a cue, not an interruption.
NARRATION_LUFS = -16.0
MUSIC_LUFS = -20.0

# Crossfade lengths (seconds). The intro overlap is longer so the music ducks
# under the opening line rather than stopping dead before it.
INTRO_XFADE = 1.5
OUTRO_XFADE = 1.0

AFORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def build_episode(narration: Path, out_path: Path,
                  intro: Path | None = None, outro: Path | None = None) -> None:
    """Wrap `narration` with the stings and write `out_path`.

    Falls back to copying the narration verbatim on any problem.
    """
    intro = INTRO if intro is None else intro
    outro = OUTRO if outro is None else outro
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

    # Normalise every piece to its target loudness first, so the crossfades join
    # sources that are already at a matched level.
    filters = [
        f"[{i}:a]loudnorm=I={NARRATION_LUFS if name == 'voice' else MUSIC_LUFS}"
        f":TP=-1.5:LRA=11,{AFORMAT}[{name}]"
        for name, i in chain
    ]

    cur = chain[0][0]
    for step, (name, _) in enumerate(chain[1:], start=1):
        dur = INTRO_XFADE if cur == "intro" else OUTRO_XFADE
        nxt = f"x{step}"
        filters.append(f"[{cur}][{name}]acrossfade=d={dur}:c1=tri:c2=tri[{nxt}]")
        cur = nxt

    args += ["-filter_complex", ";".join(filters), "-map", f"[{cur}]",
             "-codec:a", "libmp3lame", "-b:a", "128k", str(out_path)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        print(f"  assemble: ffmpeg failed ({result.returncode}); using bare narration",
              file=sys.stderr)
        print(f"  {result.stderr.strip()[:300]}", file=sys.stderr)
        shutil.copyfile(narration, out_path)
        return

    parts = "+".join(name for name, _ in chain)
    print(f"  assemble: {parts} -> {out_path.name} "
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
