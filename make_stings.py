#!/usr/bin/env python3
"""
Generate the intro/outro stings used by assemble.py, with nothing but ffmpeg.

Writes two short bell-like motifs (an A-major arpeggio up for the intro, down
and resolving for the outro) to $DATA_DIR/assets/. They exist so a fresh clone
has working audio buffers immediately, with no asset hunt and no API spend.

They are deliberately plain. To use real music instead, just overwrite the two
files: Pixabay's music library is CC0 (free for commercial use, no attribution)
and has thousands of podcast stings.

    python3 make_stings.py            # write intro.mp3 + outro.mp3
    python3 make_stings.py --force    # overwrite existing files
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from shutil import which

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
ASSETS_DIR = DATA_DIR / "assets"

SR = 44100
# A-major triad. Ascending opens, descending onto the low root closes.
A4, CS5, E5, A3 = 440.00, 554.37, 659.25, 220.00

# (frequency, start offset in seconds, how long it rings)
INTRO_NOTES = [(A4, 0.00, 1.8), (CS5, 0.20, 1.8), (E5, 0.40, 2.8)]
OUTRO_NOTES = [(E5, 0.00, 1.6), (CS5, 0.18, 1.6), (A4, 0.36, 1.8), (A3, 0.54, 2.6)]

PAD_FREQ = 110.0          # one octave below the root, for body under the bells
PAD_GAIN = 0.16
FUNDAMENTAL_GAIN = 0.42
OCTAVE_GAIN = 0.13        # a quiet octave partial is what makes a sine read as a bell


def _sting_args(notes: list[tuple[float, float, float]], out_path: Path) -> list[str]:
    total = max(start + ring for _, start, ring in notes)
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []
    labels: list[str] = []
    idx = 0

    for n, (freq, start, ring) in enumerate(notes):
        for partial, (mult, gain) in enumerate(
                ((1.0, FUNDAMENTAL_GAIN), (2.0, OCTAVE_GAIN))):
            args += ["-f", "lavfi", "-i",
                     f"sine=frequency={freq * mult:.2f}:duration={ring:.2f}:sample_rate={SR}"]
            label = f"n{n}p{partial}"
            # Exponential fade-out from almost the first sample is the decay
            # envelope; a struck bell has no sustain.
            filters.append(
                f"[{idx}:a]volume={gain},"
                f"afade=t=in:st=0:d=0.01,"
                f"afade=t=out:st=0.02:d={ring - 0.02:.2f}:curve=exp,"
                f"adelay={int(start * 1000)}[{label}]"
            )
            labels.append(label)
            idx += 1

    args += ["-f", "lavfi", "-i",
             f"sine=frequency={PAD_FREQ}:duration={total:.2f}:sample_rate={SR}"]
    filters.append(
        f"[{idx}:a]volume={PAD_GAIN},afade=t=in:st=0:d=0.35,"
        f"afade=t=out:st={max(total - 1.4, 0.1):.2f}:d=1.4[pad]"
    )
    labels.append("pad")

    joined = "".join(f"[{lbl}]" for lbl in labels)
    filters.append(
        f"{joined}amix=inputs={len(labels)}:normalize=0,"
        f"alimiter=limit=0.95,"
        f"aformat=sample_fmts=fltp:sample_rates={SR}:channel_layouts=stereo,"
        f"loudnorm=I=-18:TP=-1.5:LRA=11[out]"
    )

    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-codec:a", "libmp3lame", "-b:a", "192k", str(out_path)]
    return args


def make(name: str, notes: list[tuple[float, float, float]], force: bool) -> bool:
    out_path = ASSETS_DIR / f"{name}.mp3"
    if out_path.exists() and not force:
        print(f"  {out_path} already exists (use --force to overwrite)")
        return True
    result = subprocess.run(_sting_args(notes, out_path), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED to write {name}.mp3:\n{result.stderr.strip()[:400]}", file=sys.stderr)
        return False
    print(f"  wrote {out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Generate intro/outro stings with ffmpeg")
    p.add_argument("--force", action="store_true", help="Overwrite existing stings")
    args = p.parse_args()

    if not which("ffmpeg"):
        sys.exit("ERROR: ffmpeg is not installed; cannot generate stings.")

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Writing stings to {ASSETS_DIR}")
    ok = make("intro", INTRO_NOTES, args.force)
    ok = make("outro", OUTRO_NOTES, args.force) and ok
    if not ok:
        sys.exit(1)
    print("Done. Overwrite either file with your own music any time.")


if __name__ == "__main__":
    main()
