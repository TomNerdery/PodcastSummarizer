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

import argparse
import json
import sys
from pathlib import Path

import requests

from tts_elevenlabs import get_anthropic_key, ANTHROPIC_URL, HERE, DATA_DIR

SUMMARY_MODEL = "claude-sonnet-4-6"
PROMPT_FILE = HERE / "summary-prompt.md"

FALLBACK_PROMPT = (
    "You are a producer for a personal daily podcast called '{SHOW_NAME}'. "
    "Turn the supplied long-form podcast transcript into a single NPR-style radio "
    "essay, ~450-500 words (about 3 minutes), to be read aloud by one narrator. "
    "Open with the show sign-on. Close with a sign-off written EXACTLY as "
    "'I'm {{REPORTER}}, for {SHOW_NAME}.' Leave {{REPORTER}} untouched, braces and "
    "all: the narrator is cast after this script is written and the pipeline "
    "substitutes the real name. Do not invent a name. Lead with the single "
    "most important idea, then 3-4 key takeaways, then a brief 'so what' close. "
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


def generate_script(transcript: str, title: str = "", description: str = "",
                    model: str = SUMMARY_MODEL) -> str:
    if not transcript or not transcript.strip():
        raise ValueError("Empty transcript passed to generate_script().")

    instructions = load_prompt().replace("{SHOW_NAME}", show_name())
    user_content = (
        f"TITLE: {title}\n\n"
        f"DESCRIPTION (may include chapter markers — use as an outline):\n{description}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )
    headers = {
        "x-api-key": get_anthropic_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1500,
        "system": instructions,
        "messages": [{"role": "user", "content": user_content}],
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Claude error {resp.status_code}: {resp.text[:500]}")
    return resp.json()["content"][0]["text"].strip()


def main() -> None:
    p = argparse.ArgumentParser(description="Summarize a transcript into a radio script")
    p.add_argument("transcript", help="Path to the transcript .txt file")
    p.add_argument("--meta", help="Optional <id>.json with title/description")
    p.add_argument("--out", default="script.txt", help="Output script path")
    p.add_argument("--model", default=SUMMARY_MODEL, help="Claude model")
    args = p.parse_args()

    text = Path(args.transcript).read_text(encoding="utf-8")
    title = description = ""
    if args.meta and Path(args.meta).exists():
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        title, description = meta.get("title", ""), meta.get("description", "")

    script = generate_script(text, title, description, args.model)
    Path(args.out).write_text(script, encoding="utf-8")
    print(f"Wrote {args.out}  ({len(script.split())} words)")


if __name__ == "__main__":
    main()
