#!/usr/bin/env python3
"""
Re-render published episodes after a change to the stings or the assembly.

Two paths per episode, and it always prefers the free one:

  1. A narration master (<base>.narration.mp3) sits next to the episode, so the
     episode is simply reassembled. Costs nothing.
  2. No master (anything voiced before masters were kept), so the script is
     re-sent to ElevenLabs using the SAME voice already recorded in the
     manifest. Costs credits. This is what --pay is for.

Reusing the recorded voice matters: the script's sign-off already names that
voice's reporter, so casting a different one would put the wrong name in the
narrator's mouth.

    python3 revoice.py --since 2026-08-07 --dry-run
    python3 revoice.py --since 2026-08-07 --pay
    python3 revoice.py --all                     # free reassembly, skips the rest

Then:  python3 build_feed.py && python3 publish.py
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from assemble import NARRATION_SUFFIX, build_episode, have_stings, pair_named
from runner import DATA_DIR, EPISODES_DIR, MANIFEST, load_json, save_json
from tts_elevenlabs import DEFAULT_MODEL, QuotaExceeded, TTSError, get_api_key, synthesize

# Measured, not assumed. A 518-word script (~3,160 chars) was quoted at 1,576
# credits by the API's own quota error, which is 0.499 credits per character.
# ElevenLabs bills discounted models at 0.5 to 1.0 per character, and this
# account is on the discounted end; assuming 1.0 doubles every estimate.
CREDITS_PER_CHAR = 0.5


def narration_for(entry: dict) -> Path | None:
    """The kept master for this episode, if there is one."""
    rel = entry.get("narration_file")
    if rel and (DATA_DIR / rel).exists():
        return DATA_DIR / rel
    mp3 = entry.get("mp3_file")
    if mp3:
        guess = DATA_DIR / mp3.replace(".mp3", NARRATION_SUFFIX)
        if guess.exists():
            return guess
    return None


def script_for(entry: dict) -> Path | None:
    rel = entry.get("script_file")
    path = DATA_DIR / rel if rel else None
    return path if path and path.exists() else None


def main() -> None:
    p = argparse.ArgumentParser(description="Re-render episodes after an assembly change")
    p.add_argument("--since", help="Only episodes dated on or after this (YYYY-MM-DD)")
    p.add_argument("--all", action="store_true", help="Every episode in the manifest")
    p.add_argument("--pay", action="store_true",
                   help="Allow re-voicing (spends ElevenLabs credits) when no master exists")
    p.add_argument("--dry-run", action="store_true", help="Report only, change nothing")
    args = p.parse_args()

    if not (args.since or args.all):
        p.error("give --since YYYY-MM-DD or --all")
    if not have_stings():
        sys.exit(f"ERROR: no stings in {DATA_DIR / 'assets'}; run make_stings.py first.")

    episodes = load_json(MANIFEST, [])
    targets = [e for e in episodes
               if args.all or (e.get("date", "") >= args.since)]
    if not targets:
        sys.exit("Nothing matched.")

    free = [e for e in targets if narration_for(e)]
    paid = [e for e in targets if not narration_for(e) and script_for(e)]
    stuck = [e for e in targets if not narration_for(e) and not script_for(e)]

    chars = sum(len(script_for(e).read_text(encoding="utf-8")) for e in paid)
    print(f"Matched {len(targets)} episode(s):")
    print(f"  {len(free):>3} reassemble from kept narration   (free)")
    print(f"  {len(paid):>3} need re-voicing                  "
          f"(~{int(chars * CREDITS_PER_CHAR):,} credits)")
    if stuck:
        print(f"  {len(stuck):>3} unrecoverable (no master, no script)")
    if paid and not args.pay and not args.dry_run:
        # Report and carry on rather than refusing the whole run. Aborting here
        # made --all useless the moment a single masterless episode existed, and
        # the advice it gave ("drop --since") was backwards: --since is the only
        # thing that excludes them. The loop below already skips what it cannot
        # do for free.
        print(f"\n{len(paid)} episode(s) need re-voicing and will be SKIPPED "
              f"(~{int(chars * CREDITS_PER_CHAR):,} credits). "
              f"Re-run with --pay to include them.")
    print()

    el_key = get_api_key() if (paid and args.pay and not args.dry_run) else None
    done = 0
    for entry in targets:
        mp3 = DATA_DIR / entry["mp3_file"]
        master = narration_for(entry)
        label = entry.get("title", entry.get("video_id", "?"))[:52]

        if master:
            print(f"  [free] {label}")
            if not args.dry_run:
                # Put it back on the tune it was published with. Absent from the
                # manifest (anything from before that was recorded) means falling
                # through to the rotation, which will change the music.
                intro, outro = pair_named(entry.get("sting") or "")
                tune = build_episode(master, mp3, intro, outro)
                if tune:
                    entry["sting"] = tune      # backfill as episodes are touched
                entry["assembled"] = True
                done += 1
            continue

        script = script_for(entry)
        if not script:
            print(f"  [skip] {label}  (no master, no script)")
            continue
        if not args.pay:
            print(f"  [pay ] {label}  (would re-voice)")
            continue
        print(f"  [pay ] {label}  voice={entry.get('voice','?')[:12]}")
        if args.dry_run:
            continue

        # Re-voice into a master this time, so this episode is free to fix later.
        master = EPISODES_DIR / (Path(entry["mp3_file"]).stem + NARRATION_SUFFIX)
        try:
            synthesize(el_key, script.read_text(encoding="utf-8"), master,
                       entry.get("voice") or "", DEFAULT_MODEL)
        except QuotaExceeded as e:
            print(f"\n  STOPPING: {e}")
            break
        except TTSError as e:
            print(f"    ERROR: {e}")
            continue
        tune = build_episode(master, mp3, *pair_named(entry.get("sting") or ""))
        if tune:
            entry["sting"] = tune
        entry["narration_file"] = str(master.relative_to(DATA_DIR))
        entry["assembled"] = True
        done += 1

    if not args.dry_run:
        save_json(MANIFEST, episodes)
    print(f"\n{'(dry run) ' if args.dry_run else ''}Re-rendered {done} episode(s).")
    if done and not args.dry_run:
        print("Next: python3 build_feed.py && python3 publish.py")


if __name__ == "__main__":
    main()
