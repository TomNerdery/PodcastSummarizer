#!/usr/bin/env python3
"""
Convert a narration text file into an MP3 using the ElevenLabs API.

The API key is read from the ELEVENLABS_API_KEY environment variable
(or from a .env file sitting next to this script). The key is never
hard-coded and never printed.

Voice selection modes (--voice-mode):
    fixed   use --voice (or the config's default_voice)   [default]
    rotate  cycle through voices.json "rotation", remembering place between runs
    random  pick a random voice from "rotation"
    match   pick the voice whose keywords best fit the script's content

Setup:
    pip install requests
    export ELEVENLABS_API_KEY="your_key_here"

Common usage:
    python3 tts_elevenlabs.py --init-config          # scaffold voices.json from your voices
    python3 tts_elevenlabs.py --list-voices
    python3 tts_elevenlabs.py --preview              # download free sample clips
    python3 tts_elevenlabs.py in.txt out.mp3 --voice-mode rotate
    python3 tts_elevenlabs.py in.txt out.mp3 --voice-mode match
    python3 tts_elevenlabs.py in.txt out.mp3 --voice <VOICE_ID>
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

import requests

API_BASE = "https://api.elevenlabs.io/v1"
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
CONFIG_PATH = DATA_DIR / "voices.json"
STATE_PATH = DATA_DIR / ".voice_state.json"

# Fallback voice ("Brian") if no config/voice is given.
DEFAULT_VOICE_ID = "nPczCjzI2devNBz1zQrb"
DEFAULT_MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"

# Claude is used for --voice-mode smart (and, later, summarization).
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


# ----------------------------- key / env -----------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str:
    load_dotenv(HERE / ".env")
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit(
            "ERROR: ELEVENLABS_API_KEY is not set.\n"
            'Set it with:  export ELEVENLABS_API_KEY="your_key_here"\n'
            "or put  ELEVENLABS_API_KEY=your_key_here  in a .env file next to this script."
        )
    return key


def fetch_voices(api_key: str) -> list:
    resp = requests.get(f"{API_BASE}/voices", headers={"xi-api-key": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("voices", [])


def get_anthropic_key() -> str:
    load_dotenv(HERE / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY is not set (needed for --voice-mode smart).\n"
            'Set it with:  export ANTHROPIC_API_KEY="your_key_here"\n'
            "or add  ANTHROPIC_API_KEY=your_key_here  to the .env file."
        )
    return key


# ----------------------------- config -----------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def init_config(api_key: str) -> None:
    """Scaffold voices.json from the voices in your account. Edit it afterward."""
    if CONFIG_PATH.exists():
        sys.exit(f"{CONFIG_PATH.name} already exists; edit it directly (won't overwrite).")
    voices = fetch_voices(api_key)
    # Skip the stock premade voices so you start from the ones you added.
    mine = [v for v in voices if v.get("category") != "premade"] or voices
    rotation = [v["voice_id"] for v in mine]
    match = [
        {
            "voice_id": v["voice_id"],
            "name": v.get("name", ""),
            "keywords": [],  # <-- fill these in, e.g. ["markets","stocks","economy"]
        }
        for v in mine
    ]
    config = {
        "default_voice": rotation[0] if rotation else DEFAULT_VOICE_ID,
        "rotation": rotation,
        "match": match,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(f"Wrote {CONFIG_PATH} with {len(mine)} voices.")
    print("Next: open it and add keywords to each voice for --voice-mode match.")


# ----------------------------- voice selection -----------------------------

def choose_voice(mode: str, text: str, cli_voice: str, config: dict) -> str:
    default = config.get("default_voice") or cli_voice or DEFAULT_VOICE_ID
    rotation = config.get("rotation") or []

    if mode == "fixed":
        return cli_voice or default

    if mode in ("rotate", "random"):
        if not rotation:
            sys.exit("voice-mode needs a 'rotation' list in voices.json (run --init-config).")
        if mode == "random":
            return random.choice(rotation)
        # rotate: advance a persisted index
        idx = 0
        if STATE_PATH.exists():
            try:
                idx = json.loads(STATE_PATH.read_text()).get("next", 0)
            except (json.JSONDecodeError, OSError):
                idx = 0
        voice = rotation[idx % len(rotation)]
        STATE_PATH.write_text(json.dumps({"next": (idx + 1) % len(rotation)}))
        return voice

    if mode == "match":
        rules = config.get("match") or []
        if not rules:
            sys.exit("voice-mode match needs a 'match' list in voices.json (run --init-config).")
        words = re.findall(r"[a-z']+", text.lower())
        counts = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        best, best_score = None, 0
        for r in rules:
            score = sum(counts.get(k.lower(), 0) for k in r.get("keywords", []))
            if score > best_score:
                best, best_score = r["voice_id"], score
        if best:
            return best
        print("No keyword matches; using default voice.", file=sys.stderr)
        return default

    sys.exit(f"Unknown voice-mode: {mode}")


def build_candidates(config: dict, el_key: str) -> list:
    """Combine the user's chosen voices with rich descriptions from ElevenLabs."""
    catalog = {v["voice_id"]: v for v in fetch_voices(el_key)}
    ids = [r["voice_id"] for r in config.get("match", [])] or config.get("rotation", [])
    if not ids:
        ids = [vid for vid, v in catalog.items() if v.get("category") != "premade"]
    candidates = []
    for vid in ids:
        v = catalog.get(vid, {})
        labels = v.get("labels", {})
        desc = v.get("description") or ", ".join(f"{k}: {val}" for k, val in labels.items())
        candidates.append({"voice_id": vid, "name": v.get("name", vid), "description": desc})
    return candidates


def smart_match(text: str, candidates: list, anth_key: str, default: str) -> str:
    """Ask Claude to choose the best-fitting voice for this script."""
    if not candidates:
        sys.exit("No candidate voices found (run --init-config and add voices).")
    roster = "\n".join(
        f'- id: {c["voice_id"]} | name: {c["name"]} | description: {c["description"] or "n/a"}'
        for c in candidates
    )
    excerpt = text[:3000]
    prompt = (
        "You are casting a narrator for a short radio-news segment. "
        "Choose the single best-fitting voice from the roster for this script, "
        "considering tone, subject matter, and mood.\n\n"
        f"AVAILABLE VOICES:\n{roster}\n\n"
        f"SCRIPT:\n{excerpt}\n\n"
        'Respond ONLY with JSON: {"voice_id": "<id from the roster>", "reason": "<one sentence>"}'
    )
    headers = {
        "x-api-key": anth_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    valid = {c["voice_id"] for c in candidates}
    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        out = resp.json()["content"][0]["text"]
        data = json.loads(re.search(r"\{.*\}", out, re.DOTALL).group(0))
        choice = data.get("voice_id", "").strip()
        if choice in valid:
            print(f"Claude picked {choice}: {data.get('reason','').strip()}", file=sys.stderr)
            return choice
        print(f"Claude returned an unknown voice_id ({choice}); using default.", file=sys.stderr)
    except (requests.RequestException, KeyError, ValueError, AttributeError) as e:
        print(f"Smart match failed ({e}); using default voice.", file=sys.stderr)
    return default


# ----------------------------- actions -----------------------------

def list_voices(api_key: str) -> None:
    for v in fetch_voices(api_key):
        labels = v.get("labels", {})
        descr = ", ".join(f"{k}={val}" for k, val in labels.items())
        print(f"{v['voice_id']}  {v.get('name',''):<20} [{v.get('category','')}] {descr}")


def preview_voices(api_key: str, out_dir: Path) -> None:
    """Download the free pre-made sample clip for each voice. Costs no credits."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for v in fetch_voices(api_key):
        url = v.get("preview_url")
        if not url:
            continue
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in v.get("name", "voice"))
        dest = out_dir / f"{safe}__{v['voice_id']}.mp3"
        try:
            audio = requests.get(url, timeout=60)
            audio.raise_for_status()
            dest.write_bytes(audio.content)
            count += 1
            print(f"  {dest.name}")
        except requests.RequestException as e:
            print(f"  (skipped {v.get('name')}: {e})", file=sys.stderr)
    print(f"\nDownloaded {count} preview clips to {out_dir}")


def synthesize(api_key: str, text: str, out_path: Path, voice: str, model: str) -> None:
    url = f"{API_BASE}/text-to-speech/{voice}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0},
    }
    resp = requests.post(url, headers=headers, params={"output_format": OUTPUT_FORMAT},
                         json=payload, timeout=180)
    if resp.status_code != 200:
        sys.exit(f"ERROR {resp.status_code}: {resp.text[:500]}")
    out_path.write_bytes(resp.content)
    print(f"Wrote {out_path}  ({len(resp.content)/1024:.0f} KB)  voice={voice}")


# ----------------------------- main -----------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="ElevenLabs text-to-speech")
    p.add_argument("input", nargs="?", help="Path to the narration .txt file")
    p.add_argument("output", nargs="?", help="Path for the output .mp3 file")
    p.add_argument("--voice", default="", help="Explicit ElevenLabs voice ID (forces fixed)")
    p.add_argument("--voice-mode", default="fixed",
                   choices=["fixed", "rotate", "random", "match", "smart"],
                   help="How to pick the voice (default: fixed). 'smart' uses Claude.")
    p.add_argument("--model", default=DEFAULT_MODEL, help="ElevenLabs model ID")
    p.add_argument("--list-voices", action="store_true", help="List available voices and exit")
    p.add_argument("--preview", action="store_true",
                   help="Download free sample clips for all voices and exit")
    p.add_argument("--preview-dir", default="voice-previews", help="Folder for --preview clips")
    p.add_argument("--init-config", action="store_true",
                   help="Create voices.json from your account voices and exit")
    args = p.parse_args()

    api_key = get_api_key()

    if args.init_config:
        init_config(api_key)
        return
    if args.list_voices:
        list_voices(api_key)
        return
    if args.preview:
        preview_voices(api_key, Path(args.preview_dir))
        return

    if not args.input or not args.output:
        p.error("input and output are required (or use --list-voices / --preview / --init-config)")

    text = Path(args.input).read_text(encoding="utf-8").strip()
    if not text:
        sys.exit("ERROR: input file is empty.")
    if len(text) > 9500:
        print("WARNING: text is long; consider splitting into chunks.", file=sys.stderr)

    config = load_config()
    mode = "fixed" if args.voice else args.voice_mode
    if mode == "smart":
        candidates = build_candidates(config, api_key)
        default = config.get("default_voice") or DEFAULT_VOICE_ID
        voice = smart_match(text, candidates, get_anthropic_key(), default)
    else:
        voice = choose_voice(mode, text, args.voice, config)
    synthesize(api_key, text, Path(args.output), voice, args.model)


if __name__ == "__main__":
    main()
