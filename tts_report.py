#!/usr/bin/env python3
"""
Compare the narrator engines from what the pipeline already recorded.

There is nothing to schedule here. Every episode row carries `tts_provider`,
so the comparison is a query over data that is written as the trial runs, not a
measurement that has to be taken at a particular moment. Run it whenever, from
the laptop or from a pod on the cluster; the answer is the same.

    python3 tts_report.py                       # everything
    python3 tts_report.py --since 2026-08-23    # just the trial

Pure arithmetic on purpose: no model call, no network, no credentials. A report
that needs an LLM to add up two columns is a report that can fail for reasons
that have nothing to do with the numbers.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)

# Both verified from the vendors' own pricing pages on 2026-08-23 and recorded
# with that date in the vault, because a number quoted from memory is how you
# end up recommending something on stale pricing.
DEEPGRAM_PER_1K_CHARS = 0.030          # Aura-2
ELEVENLABS_CREDITS_PER_CHAR = 0.5      # measured from the API's own quota error

# Michael confirmed on 2026-08-23 that he is on CREATOR. That had been unknown
# all through the comparison work, which is why every cost figure written before
# this was quoted as a range. It is the number that matters most: the argument
# for changing engines was never the money, it was this ceiling.
PLAN = "Creator ($22/mo)"
PLAN_CREDITS = 121_000
NEXT_PLAN = "Pro ($99/mo)"
NEXT_PLAN_CREDITS = 600_000
CREDITS_PER_EPISODE = 1_570        # measured, not assumed


def chars_for(ep: dict) -> int:
    """Characters narrated, from the script if it survives, else estimated.

    Prefers the real file: `summary_words` is a word count, and words-to-
    characters varies enough across scripts that using it for a spend figure
    would be inventing precision.
    """
    rel = ep.get("script_file")
    if rel:
        p = DATA_DIR / rel
        if p.exists():
            return len(p.read_text(encoding="utf-8"))
    words = ep.get("summary_words") or 0
    return int(words * 6.3)            # measured ratio, only used as a fallback


def main() -> None:
    ap = argparse.ArgumentParser(description="ElevenLabs vs Deepgram, from the manifest")
    ap.add_argument("--since", help="ISO date, e.g. 2026-08-23")
    ap.add_argument("--manifest", default=str(DATA_DIR / "episodes.json"))
    args = ap.parse_args()

    episodes = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if args.since:
        episodes = [e for e in episodes
                    if (e.get("created_at") or e.get("date") or "") >= args.since]
    if not episodes:
        print("No episodes in range.")
        return

    by = {"elevenlabs": [], "deepgram": [], "unrecorded": []}
    for e in episodes:
        by.setdefault(e.get("tts_provider") or "unrecorded", []).append(e)

    print(f"{len(episodes)} episode(s)"
          + (f" since {args.since}" if args.since else "") + "\n")

    est = 0
    for name, eps in by.items():
        if not eps:
            continue
        chars = sum(chars_for(e) for e in eps)
        est += sum(1 for e in eps if not (DATA_DIR / (e.get("script_file") or "x")).exists())
        print(f"  {name:12} {len(eps):3} episode(s)   {chars:,} characters")
        if name == "deepgram":
            print(f"               cost: ${chars / 1000 * DEEPGRAM_PER_1K_CHARS:.2f}")
        elif name == "elevenlabs":
            credits = chars * ELEVENLABS_CREDITS_PER_CHAR
            print(f"               credits: {credits:,.0f}")
            print(f"               = {credits / PLAN_CREDITS * 100:.1f}% of the "
                  f"{PLAN} monthly allowance of {PLAN_CREDITS:,}")
    if est:
        print(f"\n  ({est} episode(s) had no script file; characters estimated from word count)")

    # What it WOULD have cost the other way round, which is the actual question
    total_chars = sum(chars_for(e) for e in episodes)
    print(f"\n  If every one of these had been Deepgram:  "
          f"${total_chars / 1000 * DEEPGRAM_PER_1K_CHARS:.2f}")
    print(f"  If every one had been ElevenLabs:         "
          f"{total_chars * ELEVENLABS_CREDITS_PER_CHAR:,.0f} credits")

    # Only episodes made after the field existed can say anything about
    # alternation. Judging pre-trial rows as "not alternating" is the report
    # lying about its own coverage.
    tagged = [e for e in sorted(episodes, key=lambda x: x.get("created_at") or x.get("date") or "")
              if e.get("tts_provider")]
    if len(tagged) < 2:
        print(f"\n  alternation: not enough tagged episodes yet "
              f"({len(tagged)} of {len(episodes)} carry tts_provider; the rest predate the field)")
    else:
        order = [e["tts_provider"] for e in tagged]
        flips = sum(1 for i in range(1, len(order)) if order[i] != order[i - 1])
        print(f"\n  alternation: {' '.join(o[:2] for o in order)}")
        print(f"  {flips} change(s) across {len(order)} tagged episode(s)"
              + ("  <- strictly alternating" if flips == len(order) - 1
                 else "  <- NOT strictly alternating, worth a look"))

    print("\n  voices used:")
    for name, eps in by.items():
        if not eps:
            continue
        v = Counter(e.get("voice_name") or e.get("voice") or "?" for e in eps)
        print(f"    {name:12} {', '.join(f'{k}x{n}' for k, n in v.most_common())}")

    print("\n  segment forms:")
    forms = Counter(e.get("format") or "?" for e in episodes)
    print(f"    {', '.join(f'{k}x{n}' for k, n in forms.most_common())}")

    # The ceiling, which is the whole reason this trial exists.
    ceiling = PLAN_CREDITS // CREDITS_PER_EPISODE
    el_eps = len(by.get("elevenlabs", []))
    all_eps = len(episodes)
    print(f"\n  THE CEILING, which is the actual argument:")
    print(f"    {PLAN} buys {PLAN_CREDITS:,} credits = about {ceiling} episodes a month.")
    print(f"    Next step is {NEXT_PLAN}, a {(99-22)}-dollar jump, for "
          f"{NEXT_PLAN_CREDITS // CREDITS_PER_EPISODE} episodes.")
    # Use the MEASURED average for this set, not the constant. 1,570 came from
    # one ~3,160-character script; real scripts vary and the difference moves
    # the ceiling by a lot, which is the number the decision turns on.
    if all_eps:
        avg = total_chars / all_eps * ELEVENLABS_CREDITS_PER_CHAR
        real_ceiling = int(PLAN_CREDITS / avg) if avg else 0
        print(f"    Measured here: {avg:,.0f} credits per episode, "
              f"not the {CREDITS_PER_EPISODE:,} rule of thumb.")
        print(f"    So the real ceiling is about {real_ceiling} episodes a month, "
              f"not {ceiling}.")
        allcost = total_chars * ELEVENLABS_CREDITS_PER_CHAR
        print(f"    These {all_eps} episodes all-ElevenLabs = {allcost:,.0f} credits, "
              f"{allcost / PLAN_CREDITS * 100:.0f}% of one month's allowance.")


if __name__ == "__main__":
    main()
