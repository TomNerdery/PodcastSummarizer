#!/usr/bin/env python3
"""
Segment forms — the *shape* of an episode, rotated so the show does not read
like one template with different words in it.

The summary prompt used to prescribe a fixed skeleton (sign-on, source credit,
lead idea, three or four takeaways, so-what close, sign-off) and every episode
was poured into it. Rewording that prompt cannot fix the sameness, because the
sameness is structural. So the prompt is split in two: a SPINE in
summary-prompt.md holding the rules that must never vary, and a FORM from here
that supplies the shape.

Selection is a rotation with a short avoid-list, deliberately, and NOT a model
choosing the best-fitting form. The voice caster already proved that a model
asked to pick the best fit picks the same one every day; a rolling history in
front of it is what fixed that. Same mechanism, same reason.

Config lives on DATA_DIR (formats.json) so it can be edited on the live volume
without rebuilding the image, exactly like voices.json. formats.example.json
next to the code is the committed starting roster and the fallback.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
CONFIG_PATH = DATA_DIR / "formats.json"
EXAMPLE_PATH = HERE / "formats.example.json"
STATE_PATH = DATA_DIR / ".format_state.json"

# How many recent forms to steer away from. Smaller than the voice roster's six
# because the roster itself is smaller; three of eight still leaves five.
RECENT_KEEP = 3

# What the pipeline falls back to if no config can be read at all. This is the
# format the show used before any of this existed, so the failure mode is
# "yesterday's behaviour", not a broken episode.
BUILTIN = {
    "name": "brief",
    "words": [450, 500],
    "requires": [],
    "prompt": (
        "Open with the show sign-on. Credit the source within the first two "
        "sentences: name the original show and the main guest. Lead with the "
        "single most important idea, then three or four key takeaways, then a "
        "brief 'so what should the listener do or think' close."
    ),
}

BUILTIN_SIGN_ON = "From the driver's seat, this is {SHOW_NAME}."


# ----------------------------- config -----------------------------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_config() -> dict:
    """The live roster, falling back to the committed example, then to builtin."""
    for path in (CONFIG_PATH, EXAMPLE_PATH):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("formats"):
            return data
    print("  (no formats.json or formats.example.json; using the builtin form)",
          file=sys.stderr)
    return {"sign_ons": [BUILTIN_SIGN_ON], "formats": [BUILTIN]}


def all_formats(config: dict) -> list:
    return [f for f in config.get("formats", []) if f.get("name") and f.get("prompt")]


def by_name(config: dict, name: str) -> dict | None:
    for f in all_formats(config):
        if f["name"] == name:
            return f
    return None


# ----------------------------- eligibility -----------------------------

# A form may declare what the source has to contain for it to work. Only
# mechanically checkable requirements belong here: a form that needs figures can
# be gated on whether figures exist, and a form gated on something unmeasurable
# would just be a rule nobody enforces.
def _has_numbers(text: str) -> bool:
    """Enough concrete figures that a numbers-led piece will not invent any."""
    return len(re.findall(r"\b\d[\d,.]*\s*(?:percent|%|billion|million|thousand)?\b",
                          text)) >= 8


CHECKS = {"numbers": _has_numbers}


def eligible(config: dict, source_text: str) -> list:
    out = []
    for f in all_formats(config):
        needs = f.get("requires") or []
        if all(CHECKS.get(n, lambda _t: True)(source_text) for n in needs):
            out.append(f)
    return out or [BUILTIN]


# ----------------------------- history -----------------------------

def _read_state() -> dict:
    data = _read_json(STATE_PATH)
    return data if isinstance(data, dict) else {}


def load_recent() -> list:
    """Form names used most recently, newest first."""
    recent = _read_state().get("recent", [])
    return list(recent) if isinstance(recent, list) else []


def remember(name: str) -> None:
    data = _read_state()
    recent = [n for n in data.get("recent", []) if n != name]
    recent.insert(0, name)
    data["recent"] = recent[:RECENT_KEEP]
    try:
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:  # a read-only volume must not cost us an episode
        print(f"  (could not persist format history: {e})", file=sys.stderr)


# ----------------------------- selection -----------------------------

def pick(config: dict, source_text: str, avoid: list | None = None) -> dict:
    """Choose a form: eligible, minus the recently used, at random."""
    pool = eligible(config, source_text)
    avoid = avoid if avoid is not None else load_recent()
    fresh = [f for f in pool if f["name"] not in avoid]
    # With a small roster the avoid-list can empty the pool. Falling back to the
    # full pool is right: a repeat beats no episode.
    return random.choice(fresh or pool)


def sign_on(config: dict) -> str:
    """One of the show's identification lines, so it is not the same sentence."""
    lines = [s for s in config.get("sign_ons", []) if isinstance(s, str) and s.strip()]
    return random.choice(lines) if lines else BUILTIN_SIGN_ON


def length_hint(form: dict) -> str:
    lo, hi = (form.get("words") or BUILTIN["words"])[:2]
    return f"{lo}-{hi} words"
