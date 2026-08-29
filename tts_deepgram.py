#!/usr/bin/env python3
"""
Deepgram Aura text-to-speech, as an alternative narrator to ElevenLabs.

Why this exists: ElevenLabs is ~72% of an episode's cost, but the saving is
only $100-150/year, so cost is NOT the reason. The reason is the plan ceiling:
at ~1,570 credits an episode, the $22 Creator plan covers 77 episodes a month
and the next step is $99. Hourly polling and the any-source and researched-topic
work all push at that ceiling. Aura-2 does 100 episodes for about $9.50 with no
tier at all. Full comparison in the vault: "The Gist - Text to Speech Options".

Deliberately a PEER of tts_elevenlabs, not a replacement. Same synthesize()
shape, same exception types, so the runner can pick per episode and the two can
be mixed across a trial week and compared on cost and on ear.

THE THING THAT MAKES THIS NON-TRIVIAL: /v1/speak refuses anything over 2,000
characters, and scripts run 2,650-3,160. So every episode is split and rejoined,
and a join inside the narration is the one place this pipeline has repeatedly
been hurt (the intro crossfade that ate 6.8 dB off an opening line; the sting
that cut instead of ducking). Hence:

  - splits happen on SENTENCE boundaries, so any residual pause lands where a
    pause belongs rather than mid-clause
  - the join is a sample-accurate concat with NO inserted silence and NO
    per-chunk loudness normalisation

That last point matters. assemble_parts() in assemble.py does insert a silence
and does loudnorm each segment separately, which is correct for joining a clip
to narration and would be exactly wrong here: it would manufacture a pause
mid-paragraph and let the two halves drift apart in level. Reusing it would
have created the very seam this is trying to avoid.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

# One exception hierarchy across providers, so runner.py's existing
# `except QuotaExceeded` / `except TTSError` handling works unchanged.
from tts_elevenlabs import QuotaExceeded, TTSError, load_dotenv

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
CONFIG_PATH = DATA_DIR / "voices_deepgram.json"
EXAMPLE_PATH = HERE / "voices_deepgram.example.json"

API_URL = "https://api.deepgram.com/v1/speak"

# Ask for LOSSLESS, not mp3, and encode once at the end.
#
# Measured, not assumed: Deepgram's mp3 output came back 24 kHz mono at 48 kbps,
# against ElevenLabs' mp3_44100_128. Their docs confirm mp3 is FIXED at 22050 Hz
# with a 48 kbps ceiling and cannot be raised, while linear16 and FLAC are
# configurable to 48 kHz. Requesting mp3 was therefore throwing away most of the
# band before the audio ever reached the pipeline, and no amount of resampling
# afterwards puts it back.
#
# So: pull linear16 at 44.1 kHz, join it losslessly, and make exactly one lossy
# encode at the end, at the same 128 kbps the ElevenLabs path already uses. That
# also means the chunk join happens on uncompressed samples, which is the one
# place a join can be guaranteed gapless.
OUT_SAMPLE_RATE = 44100
OUT_BITRATE = "128k"

# Measured on real scripts: Thalia reads 1,733 characters in 103.0 seconds, so
# about 16.8 characters per second at normal pace. Used only to sanity-check
# length; see _check_length().
CHARS_PER_SECOND = 16.8
# A response shorter than this fraction of the expected length is treated as
# truncated rather than as a fast read.
MIN_LENGTH_RATIO = 0.80
DEFAULT_MODEL = "aura-2"          # the family; the voice supplies the rest
DEFAULT_VOICE = "thalia"

# The hard API limit is 2,000. Splitting at 1,800 leaves room for a long final
# sentence rather than forcing a mid-clause break to satisfy the cap.
MAX_CHARS = 1800

# Deepgram says "The Gist" with a hard G, as in "guest". ElevenLabs gets it
# right. Deepgram's docs are explicit that there is no SSML and no phoneme
# control, and their own advice is to spell the word the way it should sound,
# so that is what this does.
#
# CRITICAL: this is applied ONLY to the text handed to the API. The script on
# disk keeps the real spelling, because build_feed.py republishes it as the
# episode's show notes, and "The Jist" in writing would be worse than a hard G
# in audio. Say it one way, spell it another.
SAY_AS = [
    ("The Gist", "The Jist"),
    ("the Gist", "the Jist"),
]


def for_speech(text: str) -> str:
    """The text as it should be SPOKEN, which is not how it is written."""
    for written, spoken in SAY_AS:
        text = text.replace(written, spoken)
    return text


def get_api_key() -> str:
    """Env var first, then the local file.

    The env var is what a cluster run uses, from the the-gist-env Secret. The
    file is how it works on a local dev machine, where the key may already
    exist from other work. Neither path puts the value in argv or in a log.
    """
    load_dotenv(HERE / ".env")
    key = os.environ.get("DEEPGRAM_API_KEY")
    if key:
        return key.strip()
    path = Path(os.path.expanduser("~/.config/deepgram/api-key"))
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise TTSError(
        "No Deepgram key. Set DEEPGRAM_API_KEY (in the cluster: add it to the "
        "the-gist-env Secret) or put it in ~/.config/deepgram/api-key.")


# ----------------------------- chunking -----------------------------

def chunk_text(text: str, limit: int = MAX_CHARS) -> list:
    """Split under the API cap, on sentence boundaries wherever possible.

    A sentence longer than the whole limit is split on the last space that
    fits, which is ugly but has to exist: without it one runaway sentence
    would fail the episode outright.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:            # pathological single sentence
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur.strip()); cur = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].lstrip()
        if len(cur) + len(sentence) + 1 > limit:
            out.append(cur.strip()); cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur.strip():
        out.append(cur.strip())
    return [c for c in out if c]


# ----------------------------- synthesis -----------------------------

def _speak(api_key: str, text: str, voice: str, speed: float | None) -> bytes:
    params = {"model": f"aura-2-{voice}-en", "encoding": "linear16",
              "container": "wav", "sample_rate": OUT_SAMPLE_RATE}
    if speed and abs(speed - 1.0) > 1e-6:
        params["speed"] = speed
    resp = requests.post(API_URL, params=params,
                         headers={"Authorization": f"Token {api_key}",
                                  "Content-Type": "application/json"},
                         json={"text": for_speech(text)}, timeout=180)
    if resp.status_code != 200:
        body = resp.text[:500]
        # Mirrors the ElevenLabs mapping: a spend ceiling must stop the whole
        # run cleanly, anything else is one bad episode the loop can survive.
        if resp.status_code == 429 or "insufficient" in body.lower() \
                or "quota" in body.lower():
            raise QuotaExceeded(f"Deepgram quota/rate limit ({resp.status_code}): {body}")
        if resp.status_code == 413:
            raise TTSError(f"Deepgram rejected the chunk as too long, which means "
                           f"chunk_text() let something through: {body}")
        raise TTSError(f"Deepgram error {resp.status_code}: {body}")
    _check_length(resp.content, text, speed)
    return resp.content


def _wav_seconds(data: bytes) -> float:
    """Duration from the actual bytes received, not from the header.

    The header cannot be trusted here: Deepgram streams the WAV with a
    placeholder size of about 2 GB, the classic unknown-length marker, so a
    complete response and a truncated one declare exactly the same size.
    """
    import struct
    if len(data) < 44 or data[:4] != b"RIFF":
        raise TTSError("Deepgram returned something that is not a WAV.")
    channels = struct.unpack_from("<H", data, 22)[0]
    rate = struct.unpack_from("<I", data, 24)[0]
    bits = struct.unpack_from("<H", data, 34)[0]
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"data":
            payload = len(data) - (pos + 8)
            per_sec = rate * channels * bits / 8
            return payload / per_sec if per_sec else 0.0
        pos += 8 + size + (size & 1)
    raise TTSError("Deepgram WAV has no data chunk.")


def _check_length(data: bytes, text: str, speed: float | None) -> None:
    """Refuse audio that is implausibly short for the text that was sent.

    This exists because it actually happened: an identical request returned
    6.94 MB one time and 9.09 MB the next, 78.6 seconds against 103.0 for the
    same 1,733 characters. Nothing detected it. The short file had a valid
    header, joined cleanly, and would have been published as an episode that
    stops in the middle of a sentence, already paid for.

    That is the same silent-partial-result failure this pipeline has hit twice
    already: the truncated script when Sonnet 5's thinking ate max_tokens, and
    the torn processed.json that read back as empty.

    HONEST LIMIT: this catches gross truncation, not a few missing words. The
    threshold has to stay loose because real speaking rate varies with numbers,
    punctuation and the speed multiplier. It is a floor, not a checksum.
    """
    seconds = _wav_seconds(data)
    expected = len(text) / (CHARS_PER_SECOND * (speed or 1.0))
    if expected > 0 and seconds < expected * MIN_LENGTH_RATIO:
        raise TTSError(
            f"Deepgram returned {seconds:.1f}s for {len(text)} characters, "
            f"but {expected:.1f}s was expected. Treating this as a truncated "
            f"response rather than paying to publish half a sentence.")


def _join_and_encode(pieces: list, out_path: Path) -> None:
    """Join lossless WAV chunks, then make one mp3. No silence, no per-chunk gain.

    Joining on uncompressed samples is what makes the boundary genuinely
    gapless: concatenating mp3s instead carries each file's encoder delay and
    padding into the stream and can leave a small gap at every join.

    Deliberately NOT assemble_parts(): that inserts a silence between segments
    and loudness-normalises each one separately, which is right for joining a
    clip to narration and exactly wrong here. It would manufacture a pause
    mid-paragraph and let the two halves of one sentence-flow drift apart in
    level, which is the seam this whole approach exists to avoid.
    """
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, data in enumerate(pieces):
            p = Path(tmp) / f"chunk{i}.wav"
            p.write_bytes(data)
            paths.append(p)
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in paths:
            args += ["-i", str(p)]
        if len(paths) > 1:
            streams = "".join(f"[{i}:a]" for i in range(len(paths)))
            args += ["-filter_complex",
                     f"{streams}concat=n={len(paths)}:v=0:a=1[a]", "-map", "[a]"]
        args += ["-c:a", "libmp3lame", "-b:a", OUT_BITRATE,
                 "-ar", str(OUT_SAMPLE_RATE), str(out_path)]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0 or not out_path.exists():
            raise TTSError(f"Could not join Deepgram chunks: {r.stderr[:300]}")


def synthesize(api_key: str, text: str, out_path: Path, voice: str,
               model: str = DEFAULT_MODEL, speed: float | None = None) -> None:
    """Narrate `text` to `out_path`. Same shape as tts_elevenlabs.synthesize.

    `model` is accepted and ignored beyond the aura-2 family, so the runner can
    call either provider through one code path. `voice` is a bare Deepgram
    voice name such as "thalia"; `speed` is its rate multiplier, which is how
    Apollo went from rejected to kept.
    """
    if not text or not text.strip():
        raise TTSError("Empty text passed to Deepgram synthesize().")
    chunks = chunk_text(text)
    pieces = [_speak(api_key, c, voice, speed) for c in chunks]
    _join_and_encode(pieces, Path(out_path))
    joins = len(chunks) - 1
    print(f"Wrote {out_path}  ({out_path.stat().st_size/1024:.0f} KB)  "
          f"voice={voice}{f' speed={speed}' if speed else ''}  "
          f"{len(chunks)} chunk(s), {joins} join(s)", file=sys.stderr)


# ----------------------------- the roster -----------------------------
#
# Deliberately static. The ElevenLabs equivalent calls the account's API to
# discover voices; Deepgram's roster is a fixed published catalogue, so there is
# nothing to discover and one less network call that can fail mid-episode.

def load_config() -> dict:
    """The live roster, falling back to the committed example."""
    import json
    for path in (CONFIG_PATH, EXAMPLE_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("match"):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    raise TTSError(f"No Deepgram roster found at {CONFIG_PATH} or {EXAMPLE_PATH}")


def build_candidates(config: dict, _api_key: str = "") -> list:
    """Shape smart_match() already understands, so casting is shared code.

    The signature keeps the unused key argument so the runner can call either
    provider's build_candidates identically.
    """
    return [{"voice_id": v["voice_id"], "name": v.get("name", v["voice_id"]),
             "description": ", ".join(v.get("keywords", [])) or v.get("traits", "")}
            for v in config.get("match", []) if v.get("voice_id")]


def speed_for(config: dict, voice_id: str) -> float | None:
    """Per-voice rate. Apollo is only usable at 1.3; most voices want none."""
    for v in config.get("match", []):
        if v.get("voice_id") == voice_id:
            s = v.get("speed")
            return float(s) if s else None
    return None
