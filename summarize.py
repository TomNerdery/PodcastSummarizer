#!/usr/bin/env python3
"""
Summarize a podcast transcript into an NPR-style radio script using Claude.

Reads the instruction prompt from summary-prompt.md (the part after the first
'---' divider) so you can tune the style without touching code.

Setup:
    pip install requests
    export ANTHROPIC_API_KEY="your_key_here"   # or in .env

Usage as a script:
    python3 summarize.py 32u5T6lO8qk.txt --meta 32u5T6lO8qk.json --out script.txt
As a module:
    from summarize import generate_script
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

import formats
from tts_elevenlabs import (get_anthropic_key, join_text, ANTHROPIC_URL, HERE,
                            DATA_DIR)

# Sonnet 5 at $2/$10 per MTok, against Sonnet 4.6's $3/$15. Newer and cheaper;
# the introductory price became the standard one (verified August 23, 2026).
SUMMARY_MODEL = "claude-sonnet-5"
PROMPT_FILE = HERE / "summary-prompt.md"

FALLBACK_PROMPT = (
    "You are a producer for a personal daily podcast called '{SHOW_NAME}'. "
    "Turn the supplied source material into a single NPR-style radio essay, "
    "{LENGTH}, to be read aloud by one narrator.\n\n"
    "THE SHAPE OF THIS EPISODE:\n{FORM}\n\n"
    "The show sign-on is written EXACTLY as '{SIGN_ON}'. Close with a sign-off "
    "written EXACTLY as 'I'm {{REPORTER}}, for {SHOW_NAME}.' Leave {{REPORTER}} "
    "untouched, braces and all: the narrator is cast after this script is written "
    "and the pipeline substitutes the real name. Do not invent a name. "
    "Attribute claims to the speaker; do NOT invent quotes or statistics. No headers, "
    "no bullet points, no stage directions. Spell out abbreviations and numbers so they "
    "read aloud cleanly. Output only the script text."
)


def show_name() -> str:
    """The podcast's name, used in the spoken sign-on. Read from podcast.json."""
    try:
        cfg = json.loads((DATA_DIR / "podcast.json").read_text(encoding="utf-8"))
        return cfg.get("title") or "this podcast"
    except Exception:
        return "this podcast"


def load_prompt() -> str:
    if PROMPT_FILE.exists():
        txt = PROMPT_FILE.read_text(encoding="utf-8")
        # Use the content after the first horizontal-rule divider, if present.
        parts = txt.split("\n---\n", 1)
        return (parts[1] if len(parts) == 2 else txt).strip()
    return FALLBACK_PROMPT


CLIP_INSTRUCTIONS = """

CLIPS: the transcript below is timestamped, as "[seconds] text". You may include
up to {MAX_CLIPS} short excerpts of the speaker's ACTUAL audio. To place one, put a
marker alone on its own line, using seconds from the transcript:

[[CLIP 412.5-424.0]]

Rules for clips:
- Each must be between {MIN_SECS:.0f} and {MAX_SECS:.0f} seconds long, and they must not overlap.
- Choose passages that are ILLUSTRATIVE OF THE SPEAKER'S ARGUMENT. Do NOT choose
  the single most striking, revelatory or quotable line in the episode. A clip
  should support the point you are making, not substitute for the source.
- Start and end on a natural sentence boundary.
- Write a short hand-off sentence immediately before each marker, naming the
  speaker, so the transition makes sense to a listener ("Here is Grantham
  describing what he saw:").
- After the clip, carry on in your own words. Never quote the clip's wording
  again in the narration; the listener has just heard it.
- Clips are optional. If nothing is worth playing, use none.
"""


def clip_transcript(segments: list, limit: int = 120_000) -> str:
    """Transcript annotated with start times, for choosing clip boundaries."""
    lines, total = [], 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        line = f"[{seg.get('start', 0.0):.1f}] {text}"
        total += len(line) + 1
        if total > limit:
            break
        lines.append(line)
    return "\n".join(lines)


def generate_script(transcript: str, title: str = "", description: str = "",
                    model: str = SUMMARY_MODEL, segments: list | None = None,
                    form: dict | None = None, sign_on: str | None = None) -> str:
    """Write the radio script.

    Passing `segments` (timed transcript lines) switches on clip markers, so the
    script can hand off to the speaker's real audio. The caller decides whether
    clips are wanted; this only offers them when the timings exist.

    `form` is the segment shape for this episode (see formats.py). Without one
    the builtin shape is used, which is what the show did before forms existed,
    so an old caller gets old behaviour rather than a broken prompt.
    """
    if not transcript or not transcript.strip():
        raise ValueError("Empty transcript passed to generate_script().")

    form = form or formats.BUILTIN
    name = show_name()
    line = (sign_on or formats.BUILTIN_SIGN_ON).replace("{SHOW_NAME}", name)
    instructions = (load_prompt()
                    .replace("{FORM}", form["prompt"])
                    .replace("{LENGTH}", formats.length_hint(form))
                    .replace("{SIGN_ON}", line)
                    .replace("{SHOW_NAME}", name))
    body = transcript
    if segments:
        from clips import MAX_CLIP_SECONDS, MAX_CLIPS, MIN_CLIP_SECONDS
        instructions += CLIP_INSTRUCTIONS.format(
            MAX_CLIPS=MAX_CLIPS, MIN_SECS=MIN_CLIP_SECONDS, MAX_SECS=MAX_CLIP_SECONDS)
        body = clip_transcript(segments)

    user_content = (
        f"TITLE: {title}\n\n"
        f"DESCRIPTION (may include chapter markers — use as an outline):\n{description}\n\n"
        f"TRANSCRIPT:\n{body}"
    )
    headers = {
        "x-api-key": get_anthropic_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        # 1500 was sized for ~500 words of pure output. Sonnet 5 thinks before
        # it answers and that thinking is drawn from the same ceiling, so the
        # 600-650 word forms ran out mid-sentence. Only tokens actually
        # generated are billed, so headroom here is free.
        "max_tokens": 8000,
        "system": instructions,
        "messages": [{"role": "user", "content": user_content}],
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Claude error {resp.status_code}: {resp.text[:500]}")
    return join_text(resp.json())


def main() -> None:
    p = argparse.ArgumentParser(description="Summarize a transcript into a radio script")
    p.add_argument("transcript", help="Path to the transcript .txt file")
    p.add_argument("--meta", help="Optional <id>.json with title/description")
    p.add_argument("--out", default="script.txt", help="Output script path")
    p.add_argument("--model", default=SUMMARY_MODEL, help="Claude model")
    p.add_argument("--form", help="Segment form by name (default: rotate)")
    args = p.parse_args()

    text = Path(args.transcript).read_text(encoding="utf-8")
    title = description = ""
    if args.meta and Path(args.meta).exists():
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        title, description = meta.get("title", ""), meta.get("description", "")

    cfg = formats.load_config()
    form = formats.by_name(cfg, args.form) if args.form else formats.pick(cfg, text)
    if form is None:
        sys.exit(f"No such form: {args.form}")

    script = generate_script(text, title, description, args.model,
                             form=form, sign_on=formats.sign_on(cfg))
    Path(args.out).write_text(script, encoding="utf-8")
    print(f"Wrote {args.out}  ({len(script.split())} words, form={form['name']})")


if __name__ == "__main__":
    main()
