#!/usr/bin/env python3
"""
Build the intro/outro stings used by assemble.py.

Two modes:

    --from-music DIR    cut stings from your own music (recommended)
    (no flag)           synthesise a plain bell motif with ffmpeg

With --from-music, each track in DIR becomes one numbered pair: the intro is cut
from the START of the track, the outro from the END, so a single episode opens
and closes on the same tune. assemble.py then rotates through the pairs, giving
the show variety across episodes without ever mixing two tunes inside one.

    $DATA_DIR/assets/intro-1.mp3, outro-1.mp3     <- track 1
    $DATA_DIR/assets/intro-2.mp3, outro-2.mp3     <- track 2   ...

Both ends get fades regardless of how the source track begins or ends, so a
sting never slams in or stops mid-note.

    python3 make_stings.py --from-music ~/music --seconds 5
    python3 make_stings.py --from-music ~/music --force
    python3 make_stings.py                        # synthesised fallback pair
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
DEFAULT_SECONDS = 5.0
MUSIC_LUFS = -20.0

# Fades. The intro's tail is short because assemble.py crossfades it under the
# first line of narration anyway; the outro's tail is what actually ends the
# episode, so it gets longer to resolve properly.
INTRO_FADE_IN = 0.05
INTRO_FADE_OUT = 1.2
OUTRO_FADE_IN = 0.6
OUTRO_FADE_OUT = 1.8

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}

# --- synthesised fallback (A-major triad up to open, down to close) ---
A4, CS5, E5, A3 = 440.00, 554.37, 659.25, 220.00
INTRO_NOTES = [(A4, 0.00, 1.8), (CS5, 0.20, 1.8), (E5, 0.40, 2.8)]
OUTRO_NOTES = [(E5, 0.00, 1.6), (CS5, 0.18, 1.6), (A4, 0.36, 1.8), (A3, 0.54, 2.6)]
PAD_FREQ, PAD_GAIN = 110.0, 0.16
FUNDAMENTAL_GAIN, OCTAVE_GAIN = 0.42, 0.13

FINAL = (f"aformat=sample_fmts=fltp:sample_rates={SR}:channel_layouts=stereo,"
         f"loudnorm=I={MUSIC_LUFS}:TP=-1.5:LRA=11")


def run(args: list[str], what: str) -> bool:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED {what}:\n{result.stderr.strip()[:400]}", file=sys.stderr)
        return False
    return True


def duration(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


# ----------------------------- cut from music -----------------------------

def cut(src: Path, dest: Path, start: float, seconds: float,
        fade_in: float, fade_out: float) -> bool:
    filt = (f"afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={max(seconds - fade_out, 0.1):.2f}:d={fade_out},"
            f"{FINAL}")
    return run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(src),
                "-af", filt, "-codec:a", "libmp3lame", "-b:a", "192k", str(dest)],
               f"cutting {dest.name}")


def from_music(src_dir: Path, seconds: float, force: bool) -> bool:
    tracks = sorted(p for p in src_dir.iterdir()
                    if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("."))
    if not tracks:
        sys.exit(f"ERROR: no audio files found in {src_dir}")

    print(f"Cutting {seconds:g}s stings from {len(tracks)} track(s) into {ASSETS_DIR}\n")
    ok = True
    made = 0
    for n, track in enumerate(tracks, start=1):
        intro = ASSETS_DIR / f"intro-{n}.mp3"
        outro = ASSETS_DIR / f"outro-{n}.mp3"
        if intro.exists() and outro.exists() and not force:
            print(f"  {n}. {track.name[:58]}  (already cut, --force to redo)")
            made += 1
            continue

        total = duration(track)
        if total is None:
            print(f"  {n}. {track.name[:58]}  SKIPPED: unreadable", file=sys.stderr)
            ok = False
            continue
        if total < seconds * 2:
            print(f"  {n}. {track.name[:58]}  SKIPPED: only {total:.1f}s long",
                  file=sys.stderr)
            ok = False
            continue

        print(f"  {n}. {track.name[:58]}  ({total:.0f}s)")
        got = cut(track, intro, 0.0, seconds, INTRO_FADE_IN, INTRO_FADE_OUT)
        # Outro comes off the tail, so the episode ends where the tune does.
        got = cut(track, outro, total - seconds, seconds,
                  OUTRO_FADE_IN, OUTRO_FADE_OUT) and got
        if got:
            print(f"     intro-{n}.mp3 ({intro.stat().st_size / 1024:.0f} KB)"
                  f"  outro-{n}.mp3 ({outro.stat().st_size / 1024:.0f} KB)")
            made += 1
        else:
            ok = False

    # A leftover single pair would just sit there unused once numbered pairs
    # exist, so clear it rather than leaving two sources of truth.
    for stale in (ASSETS_DIR / "intro.mp3", ASSETS_DIR / "outro.mp3"):
        if made and stale.exists():
            stale.unlink()
            print(f"\n  removed superseded {stale.name}")

    print(f"\n{made} pair(s) ready. assemble.py rotates through them, one tune per episode.")
    return ok


# ----------------------------- synthesised fallback -----------------------------

def _sting_args(notes, out_path: Path) -> list[str]:
    total = max(start + ring for _, start, ring in notes)
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters, labels, idx = [], [], 0
    for n, (freq, start, ring) in enumerate(notes):
        for partial, (mult, gain) in enumerate(((1.0, FUNDAMENTAL_GAIN), (2.0, OCTAVE_GAIN))):
            args += ["-f", "lavfi", "-i",
                     f"sine=frequency={freq * mult:.2f}:duration={ring:.2f}:sample_rate={SR}"]
            label = f"n{n}p{partial}"
            filters.append(
                f"[{idx}:a]volume={gain},afade=t=in:st=0:d=0.01,"
                f"afade=t=out:st=0.02:d={ring - 0.02:.2f}:curve=exp,"
                f"adelay={int(start * 1000)}[{label}]")
            labels.append(label)
            idx += 1
    args += ["-f", "lavfi", "-i",
             f"sine=frequency={PAD_FREQ}:duration={total:.2f}:sample_rate={SR}"]
    filters.append(f"[{idx}:a]volume={PAD_GAIN},afade=t=in:st=0:d=0.35,"
                   f"afade=t=out:st={max(total - 1.4, 0.1):.2f}:d=1.4[pad]")
    labels.append("pad")
    joined = "".join(f"[{lbl}]" for lbl in labels)
    filters.append(f"{joined}amix=inputs={len(labels)}:normalize=0,"
                   f"alimiter=limit=0.95,{FINAL}[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-codec:a", "libmp3lame", "-b:a", "192k", str(out_path)]
    return args


def synth(name: str, notes, force: bool) -> bool:
    out_path = ASSETS_DIR / f"{name}.mp3"
    if out_path.exists() and not force:
        print(f"  {out_path.name} already exists (--force to overwrite)")
        return True
    if not run(_sting_args(notes, out_path), f"writing {name}.mp3"):
        return False
    print(f"  wrote {out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Build intro/outro stings")
    p.add_argument("--from-music", metavar="DIR",
                   help="Cut stings from every audio file in DIR (one pair per track)")
    p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                   help=f"Sting length in seconds (default: {DEFAULT_SECONDS:g})")
    p.add_argument("--force", action="store_true", help="Overwrite existing stings")
    args = p.parse_args()

    if not which("ffmpeg"):
        sys.exit("ERROR: ffmpeg is not installed; cannot build stings.")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    if args.from_music:
        src = Path(args.from_music).expanduser()
        if not src.is_dir():
            sys.exit(f"ERROR: {src} is not a directory")
        sys.exit(0 if from_music(src, args.seconds, args.force) else 1)

    print(f"Writing synthesised stings to {ASSETS_DIR}")
    ok = synth("intro", INTRO_NOTES, args.force)
    ok = synth("outro", OUTRO_NOTES, args.force) and ok
    if not ok:
        sys.exit(1)
    print("Done. Use --from-music to cut stings from real tracks instead.")


if __name__ == "__main__":
    main()
