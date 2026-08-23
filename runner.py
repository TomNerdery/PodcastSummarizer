#!/usr/bin/env python3
"""
Podcast Summaries runner — one command turns a YouTube video (or a whole
playlist) into a published-ready radio episode.

Per video it chains:  capture transcript -> Claude summary -> pick voice -> ElevenLabs MP3
and records the result in episodes.json (the manifest the RSS/publish step will read).

IMPORTANT: run this on your own machine (open network access). It needs to
reach YouTube, which the Cowork sandbox cannot.

Setup:
    pip install requests youtube-transcript-api yt-dlp
    # in .env (next to this file):
    #   ELEVENLABS_API_KEY=...
    #   ANTHROPIC_API_KEY=...
    #   YOUTUBE_API_KEY=...        (only needed for --playlist)

Usage:
    python3 runner.py "https://www.youtube.com/watch?v=32u5T6lO8qk"
    python3 runner.py --playlist PLxxxxxxxx          # process new items only
    python3 runner.py <id> --voice-mode rotate       # override voice selection
    python3 runner.py --playlist PLxxxx --reprocess   # ignore the processed list
    python3 runner.py <id> --dry-run --form all       # every segment form, no TTS
    python3 runner.py <id> --form cold-open           # force one shape

Each episode is written to one of the segment forms in formats.json, rotated so
a run of episodes does not sound like one template. --dry-run writes the
script(s) and stops, spending Claude tokens but no ElevenLabs credits, which is
how a format change gets judged before it costs anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import requests

import clips
import formats
import state
from assemble import NARRATION_SUFFIX, assemble_parts, build_episode, pair_named
from get_transcript import capture, parse_video_id
from summarize import SUMMARY_MODEL, generate_script, show_name
from tts_elevenlabs import (
    DEFAULT_MODEL, DEFAULT_VOICE_ID, QuotaExceeded, build_candidates, choose_voice,
    get_anthropic_key, get_api_key, load_config, load_dotenv, load_recent,
    remember_voice, reporter_for, smart_match, synthesize,
)

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
EPISODES_DIR = DATA_DIR / "episodes"
DRY_RUN_DIR = DATA_DIR / "dry-runs"
# Transcripts and scripts, kept rather than spent on one Claude call and thrown
# away. This is both the resume checkpoint and the archive TG-1 asked for.
ARCHIVE_DIR = DATA_DIR / "archive"
MANIFEST = DATA_DIR / "episodes.json"
PROCESSED = DATA_DIR / "processed.json"
YT_API = "https://www.googleapis.com/youtube/v3/playlistItems"


# YouTube publishes captions some time after a video goes up, so a video added
# to the playlist minutes ago may legitimately have none yet. Hourly polling
# makes that the common case rather than the rare one. Retiring such a video on
# the first look, which is what marking it processed does, loses the episode for
# good. Retrying costs nothing: this failure happens before any credit is spent.
MAX_TRANSCRIPT_ATTEMPTS = 6


class NoTranscript(Exception):
    """No captions available for this video *yet*. Free to retry; nothing paid."""


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:maxlen].strip("-") or "episode"


# ----------------------- state -----------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path: Path, data) -> None:
    """Write state atomically. A half-written file here is expensive.

    `load_json` swallows a decode error and returns its default, which is the
    right call for a missing file and a disaster for a truncated one:
    a torn `processed.json` reads back as "nothing processed", so the next run
    reprocesses the whole playlist and pays ElevenLabs for every episode again,
    and a torn `episodes.json` empties the published feed. Both were one
    badly-timed pod kill away from a plain `write_text`, and hourly polling
    means far more writes and far more chances to be killed during one.

    Write to a temp file beside the target, flush it all the way to disk, then
    rename. `os.replace` is atomic on POSIX, so a reader sees the old file or
    the new one and never a half of either. The temp file is in the same
    directory on purpose: rename is only atomic within one filesystem.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            # Without this the rename can land before the contents do, which
            # turns a torn write into an empty-but-valid-looking file.
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ----------------------- voice -----------------------

def pick_voice(mode: str, script: str, config: dict, el_key: str) -> str:
    """Cast a narrator, steering away from the ones used most recently.

    Without the `avoid` list every mode converges on the same one or two voices:
    'smart' because the best-fitting voice for a news read is the same one every
    day, 'random' because chance repeats. The history lives on DATA_DIR so it
    survives across runs.
    """
    default = config.get("default_voice") or DEFAULT_VOICE_ID
    avoid = load_recent()
    if mode == "smart":
        candidates = build_candidates(config, el_key)
        voice = smart_match(script, candidates, get_anthropic_key(), default, avoid=avoid)
    else:
        voice = choose_voice(mode, script, "", config, avoid=avoid)
    remember_voice(voice)
    return voice


# ----------------------- core -----------------------

def choose_form(cap: dict, form_name: str | None) -> tuple[dict, str, dict]:
    """The shape of this episode, and the sign-on line that goes with it.

    Rotating the shape is the point: one fixed skeleton is what made a run of
    episodes sound like a template. See formats.py for why this rotates rather
    than letting the model pick.
    """
    cfg = formats.load_config()
    if form_name:
        form = formats.by_name(cfg, form_name)
        if form is None:
            names = ", ".join(f["name"] for f in formats.all_formats(cfg))
            raise ValueError(f"No such form '{form_name}'. Available: {names}")
    else:
        form = formats.pick(cfg, cap.get("transcript") or "")
    # Resolved here, not left as a template, so the manifest records the line
    # that was actually spoken rather than the one that was drawn.
    line = formats.sign_on(cfg).replace("{SHOW_NAME}", show_name())
    return form, line, cfg


def archive_transcript(vid: str, cap: dict) -> Path:
    """Keep the transcript. It is the checkpoint AND the archive, in one write.

    Two reasons, and the second is the one that cannot be undone later:

    1. A resume must not refetch what it already has.
    2. A transcript can stop existing. One episode is already orphaned because
       its source video is gone from YouTube. Once that happens the episode can
       never be rebuilt, re-voiced or re-read, and no amount of state helps.

    This is why TG-1's two halves are one job: checkpointing has to persist the
    transcript, and the persisted transcript is the archive.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE_DIR / f"{vid}.transcript.json"
    save_json(out, {
        "video_id": vid,
        "title": cap.get("title", ""),
        "channel": cap.get("channel", ""),
        "description": cap.get("description", ""),
        "source": cap.get("source", ""),
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "transcript": cap.get("transcript", ""),
        "segments": cap.get("segments") or [],
    })
    return out


def load_archived(path: Path) -> dict | None:
    data = load_json(path, None)
    if not isinstance(data, dict) or not data.get("transcript"):
        return None
    return data


def process_video(video: str, mode: str, lang: str, allow_whisper: bool,
                  form_name: str | None = None,
                  conn=None) -> dict | None:
    vid = parse_video_id(video)
    print(f"\n=== {vid} ===")

    # --- resume: the one-way door ------------------------------------------
    # Narration is the only stage that costs real money, and it is irreversible.
    # If a master already exists for this item then it has been paid for, and
    # nothing below this point may buy it again. Everything after narration is
    # free to redo from the master, which is exactly what revoice.py does, so a
    # failure at assemble or publish costs nothing to recover from.
    #
    # Resume granularity is deliberately "before the door" or "after it", not
    # every stage. A resume that has no master re-runs the script step too, at
    # about seven cents. Reconstructing half-finished clip and chunk state to
    # save that would be a lot of fragile code guarding a cheap stage, and
    # fragile code around the paid stage is how you end up paying twice.
    if conn is not None:
        master = state.artifact_file(conn, vid, "narration")
        entry = next((e for e in load_json(MANIFEST, [])
                      if e.get("video_id") == vid), None)
        if master is not None and entry:
            print("  narration already paid for; re-assembling from the master")
            mp3_path = DATA_DIR / entry["mp3_file"]
            tune = build_episode(master, mp3_path, *pair_named(entry.get("sting") or ""))
            if tune:
                entry["sting"] = tune
            record_episode(entry)
            state.record_artifact(conn, vid, "mp3", path=entry["mp3_file"],
                                  size=mp3_path.stat().st_size if mp3_path.exists() else None)
            state.mark(conn, vid, "script", "done", detail="resumed")
            state.mark(conn, vid, "narrate", "done", detail="resumed")
            state.mark(conn, vid, "assemble", "done")
            state.mark(conn, vid, "publish", "done")
            state.set_status(conn, vid, state.PUBLISHED)
            return entry

    # --- stage: transcript -------------------------------------------------
    archived = ARCHIVE_DIR / f"{vid}.transcript.json"
    cap = load_archived(archived)
    if cap:
        print(f"  transcript: {len(cap['transcript'].split())} words "
              f"(archived {cap.get('captured_at', '?')}, not refetched)")
    else:
        cap = capture(video, lang=lang, allow_whisper=allow_whisper)
        if not cap.get("transcript"):
            # Raised, not returned, so the caller can tell "captions are not
            # there yet" apart from every other failure. They are not the same
            # thing: this one happens before a single credit is spent, so it is
            # the one failure that is always free to retry.
            raise NoTranscript(f"no transcript yet (try --whisper). "
                               f"source={cap.get('source')}")
        path = archive_transcript(vid, cap)
        print(f"  transcript: {len(cap['transcript'].split())} words via "
              f"{cap['source']}, archived")
        if conn is not None:
            state.record_artifact(conn, vid, "transcript",
                                  path=str(path.relative_to(DATA_DIR)),
                                  size=path.stat().st_size)
    if conn is not None:
        state.upsert_item(conn, vid, source_url=f"https://www.youtube.com/watch?v={vid}",
                          title=cap.get("title", ""), channel=cap.get("channel", ""))
        state.mark(conn, vid, "transcript", "done")

    # Timed segments are only offered to the summarizer when clips are switched
    # on, so the default path sends the same flat transcript it always has.
    segments = cap.get("segments") if clips.enabled() else None

    form, sign_on_line, _cfg = choose_form(cap, form_name)
    print(f"  form: {form['name']} ({formats.length_hint(form)})")

    def write_script(with_clips: bool) -> str:
        return generate_script(cap["transcript"], cap.get("title", ""),
                               cap.get("description", ""),
                               segments=segments if with_clips else None,
                               form=form, sign_on=sign_on_line)

    script = write_script(bool(segments))
    print(f"  script: {len(script.split())} words")

    # Only a form the rotation chose goes into the history. Remembering an
    # explicit --form would let one manual run steer the next three episodes.
    if not form_name:
        formats.remember(form["name"])

    el_key = get_api_key()
    config = load_config()
    voice = pick_voice(mode, script, config, el_key)

    # The script is written before the narrator is cast, so it carries a
    # {{REPORTER}} placeholder in the sign-off. Only now do we know who reads it.
    reporter = reporter_for(config, voice)
    script = script.replace("{{REPORTER}}", reporter)
    print(f"  reporter: {reporter}")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    date = dt.date.today().isoformat()
    base = f"{date}-{slugify(cap.get('title') or vid)}"
    script_path = EPISODES_DIR / f"{base}.txt"
    mp3_path = EPISODES_DIR / f"{base}.mp3"
    narration_path = EPISODES_DIR / f"{base}{NARRATION_SUFFIX}"

    # Cut the clips BEFORE paying for any narration. Fetching is free, so a
    # failure here costs nothing, and it must be settled first: the script's
    # hand-off lines ("here is Grantham describing...") only make sense if the
    # clip that follows actually exists. An all-or-nothing rule is what stops a
    # dangling hand-off reaching the feed.
    clip_files: list[Path] = []
    clip_spans: list[tuple[float, float]] = []
    chunks: list[str] = []
    if segments and clips.parse_script(script)[1]:
        nominated = clips.parse_script(script)[1]
        capped = clips.sanitize(nominated)
        if len(capped) == len(nominated):
            with tempfile.TemporaryDirectory() as tmp:
                made = clips.extract(vid, capped, Path(tmp))
                if len(made) == len(capped):
                    clip_files = [Path(shutil.copy(m, EPISODES_DIR / f"{base}.clip{i}.mp3"))
                                  for i, m in enumerate(made, start=1)]
                    clip_spans = capped
                    chunks = clips.parse_script(script)[0]
        if not clip_files:
            # Rewrite cleanly rather than ship a hand-off with nothing after it.
            # One extra Claude call, only on the failure path.
            print("  clips: could not place them all, rewriting without clips")
            script = write_script(False).replace("{{REPORTER}}", reporter)

    spoken = clips.strip_markers(script)
    script_path.write_text(spoken, encoding="utf-8")

    # The script and the clip decision commit together, never separately. If
    # clips fail, the script is rewritten without them; recording "script done"
    # before that is settled would let a resume replay a clip-bearing script
    # whose spoken hand-offs point at audio that was never cut. That dangling
    # hand-off is exactly what the all-or-nothing rule above exists to prevent.
    if conn is not None:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        raw = ARCHIVE_DIR / f"{vid}.script.txt"
        raw.write_text(script, encoding="utf-8")   # markers intact, for the archive
        state.record_artifact(conn, vid, "script",
                              path=str(raw.relative_to(DATA_DIR)),
                              size=raw.stat().st_size)
        state.mark(conn, vid, "script", "done",
                   detail=f"form={form['name']} clips={len(clip_files)}")

    # Narrate, then wrap with the intro/outro stings so episodes do not run
    # straight into each other in the car.
    #
    # The narration master is KEPT, not thrown away in a temp dir. Assembly is
    # free to redo, TTS is not, so any later change to the stings or the join
    # can be re-applied without paying to re-voice. Learned the hard way: a
    # crossfade bug meant 35 published episodes could only be repaired by
    # re-voicing them. With clips the master is the finished body (narration and
    # clips interleaved, still no stings), which keeps that property intact.
    if clip_files:
        with tempfile.TemporaryDirectory() as tmp:
            body: list[tuple[Path, str]] = []
            for i, chunk in enumerate(chunks):
                if chunk.strip():
                    part = Path(tmp) / f"chunk{i}.mp3"
                    synthesize(el_key, chunk, part, voice, DEFAULT_MODEL)
                    body.append((part, "voice"))
                if i < len(clip_files):  # chunk, clip, chunk, clip, chunk
                    body.append((clip_files[i], "clip"))
            assemble_parts(body, narration_path)
        print(f"  body: {sum(1 for c in chunks if c.strip())} narration part(s) "
              f"+ {len(clip_files)} clip(s) [{clips.describe(clip_spans)}]")
    else:
        synthesize(el_key, spoken, narration_path, voice, DEFAULT_MODEL)
    # Paid for and on disk. Record it before anything downstream can fail, for
    # the same reason record_episode() fires the instant synthesize() returns:
    # from here on, a failure must cost time, never credits.
    if conn is not None:
        state.record_artifact(conn, vid, "narration",
                              path=str(narration_path.relative_to(DATA_DIR)),
                              size=narration_path.stat().st_size)
        state.mark(conn, vid, "narrate", "done")

    tune = build_episode(narration_path, mp3_path)

    entry = {
        "video_id": vid,
        "title": cap.get("title", "") or vid,
        "channel": cap.get("channel", ""),
        "source_url": f"https://www.youtube.com/watch?v={vid}",
        "date": date,
        # Everything below this line exists so that a question asked months
        # later ("what did that episode use?") is answered by reading one row,
        # not by inferring it from rotation state that has since moved on.
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "voice": voice,
        "voice_name": voice_name_for(config, voice),
        "reporter": reporter,
        "model": SUMMARY_MODEL,
        "sign_on": sign_on_line,
        "transcript_words": len(cap["transcript"].split()),
        "transcript_source": cap.get("source", ""),
        # Relative to DATA_DIR, which is where these actually live. Resolving
        # against HERE (the code dir) is what silently binned every episode
        # between the k3s migration and August 7, 2026.
        "narration_file": str(narration_path.relative_to(DATA_DIR)),
        # Which tune this episode was published with. Recorded so a later
        # re-render can put it back on the same one: the rotation only knows what
        # comes next, so without this every re-rendered episode silently changes
        # its music, which is what happened to 16 of them on August 12, 2026.
        "sting": tune,
        # Which segment shape this episode was written to. Recorded so a later
        # re-render can reproduce it, and so that after a month there is real
        # evidence about which forms are worth keeping. The tune was not
        # recorded once and sixteen episodes silently changed music.
        "format": form["name"],
        "clips": [{"start": s, "end": e} for s, e in clip_spans],
        "clip_files": [str(c.relative_to(DATA_DIR)) for c in clip_files],
        "script_file": str(script_path.relative_to(DATA_DIR)),
        "mp3_file": str(mp3_path.relative_to(DATA_DIR)),
        "summary_words": len(spoken.split()),
    }
    # The audio is paid for the instant synthesize() returns, so bank it in the
    # manifest right here. Nothing downstream may discard it.
    record_episode(entry)
    if conn is not None:
        state.record_artifact(conn, vid, "mp3", path=entry["mp3_file"],
                              size=mp3_path.stat().st_size if mp3_path.exists() else None)
        for i, c in enumerate(clip_files, start=1):
            state.record_artifact(conn, vid, f"clip{i}",
                                  path=str(c.relative_to(DATA_DIR)))
        state.mark(conn, vid, "assemble", "done", detail=f"sting={tune}")
        state.mark(conn, vid, "publish", "done")
        state.set_status(conn, vid, state.PUBLISHED)
    return entry


def dry_run(video: str, lang: str, allow_whisper: bool,
            form_name: str | None) -> None:
    """Write scripts and stop. No voice, no audio, no manifest, no credits.

    This is how a format change gets judged before it costs anything. The
    transcript is captured ONCE and reused across every form, so sweeping the
    whole roster is eight Claude calls, not eight trips to YouTube.

    The scripts still carry {{REPORTER}}: the narrator is cast later, and
    casting here would be work thrown away.
    """
    vid = parse_video_id(video)
    print(f"\n=== {vid} (dry run) ===")
    cap = capture(video, lang=lang, allow_whisper=allow_whisper)
    if not cap.get("transcript"):
        print(f"  SKIP: no transcript (try --whisper). source={cap.get('source')}")
        return
    print(f"  transcript: {len(cap['transcript'].split())} words via {cap['source']}")

    cfg = formats.load_config()
    if form_name == "all":
        forms = formats.eligible(cfg, cap["transcript"])
        skipped = [f["name"] for f in formats.all_formats(cfg) if f not in forms]
        if skipped:
            print(f"  not eligible for this source: {', '.join(skipped)}")
    elif form_name:
        one = formats.by_name(cfg, form_name)
        if one is None:
            names = ", ".join(f["name"] for f in formats.all_formats(cfg))
            sys.exit(f"No such form '{form_name}'. Available: {names}, all")
        forms = [one]
    else:
        forms = [formats.pick(cfg, cap["transcript"])]

    DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
    date = dt.date.today().isoformat()
    base = f"{date}-{slugify(cap.get('title') or vid)}"
    # One sign-on for the whole sweep, so the forms are compared against each
    # other and not against a second variable moving at the same time.
    line = formats.sign_on(cfg)

    for form in forms:
        try:
            script = generate_script(cap["transcript"], cap.get("title", ""),
                                     cap.get("description", ""),
                                     form=form, sign_on=line)
        except Exception as e:  # one bad form must not lose the rest of the sweep
            print(f"  {form['name']}: ERROR {e}")
            continue
        out = DRY_RUN_DIR / f"{base}.{form['name']}.txt"
        out.write_text(clips.strip_markers(script), encoding="utf-8")
        print(f"  {form['name']:<16} {len(script.split()):>4} words  -> "
              f"{out.relative_to(DATA_DIR)}")

    print(f"\nNo audio made and no credits spent. Scripts in {DRY_RUN_DIR}")


def voice_name_for(config: dict, voice_id: str) -> str:
    """The human name of a voice. The ID alone tells you nothing months later."""
    for rule in config.get("match", []):
        if rule.get("voice_id") == voice_id and rule.get("name"):
            return rule["name"]
    return ""


def record_episode(entry: dict) -> None:
    episodes = load_json(MANIFEST, [])
    episodes = [e for e in episodes if e.get("video_id") != entry["video_id"]]
    episodes.append(entry)
    save_json(MANIFEST, episodes)


# ----------------------- playlist -----------------------

def playlist_video_ids(playlist_id: str) -> list:
    load_dotenv(HERE / ".env")
    import os
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("ERROR: YOUTUBE_API_KEY is not set (needed for --playlist).")
    ids, page = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id,
                  "maxResults": 50, "key": key}
        if page:
            params["pageToken"] = page
        r = requests.get(YT_API, params=params, timeout=30)
        if r.status_code != 200:
            sys.exit(f"YouTube API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids


# ----------------------- main -----------------------

def main() -> None:
    p = argparse.ArgumentParser(description="YouTube -> radio episode runner")
    p.add_argument("video", nargs="?", help="A YouTube URL or video ID (single-video mode)")
    p.add_argument("--playlist", help="A YouTube playlist ID (process new items)")
    p.add_argument("--voice-mode", default="smart",
                   choices=["smart", "match", "rotate", "random", "fixed"],
                   help="Voice selection (default: smart)")
    p.add_argument("--lang", default="en", help="Preferred caption language")
    p.add_argument("--whisper", action="store_true", help="Allow Whisper fallback")
    p.add_argument("--reprocess", action="store_true",
                   help="With --playlist, ignore the processed list and redo everything")
    p.add_argument("--form", help="Segment form by name, or 'all' with --dry-run "
                                  "(default: rotate, avoiding the last few used)")
    p.add_argument("--dry-run", action="store_true",
                   help="Write the script(s) and stop. No TTS, no credits spent.")
    args = p.parse_args()

    if not args.video and not args.playlist:
        p.error("provide a video URL/ID, or --playlist <ID>")

    # 'all' would otherwise voice and publish one episode per form off a single
    # video, which is eight paid narrations nobody asked for.
    if args.form == "all" and not args.dry_run:
        p.error("--form all only makes sense with --dry-run")

    if args.dry_run:
        if not args.video:
            p.error("--dry-run takes a single video, not --playlist")
        dry_run(args.video, args.lang, args.whisper, args.form)
        return

    if args.video:
        try:
            entry = process_video(args.video, args.voice_mode, args.lang, args.whisper,
                                  form_name=args.form)
        except NoTranscript as e:
            # One-off run: there is no queue to hold it in, so just say so.
            # The retry logic belongs to playlist mode, which runs again later.
            sys.exit(f"  SKIP: {e}")
        print(f"\nDone -> {entry['mp3_file']}")  # process_video already recorded it
        return

    # playlist mode
    #
    # State lives in state.db now, one row per item and one per stage, so a
    # failure at a free stage no longer has to be recorded as "done" to stop a
    # retry re-buying audio. That trade is what used to lose episodes.
    conn = state.connect()
    try:
        state.ensure_migrated(conn, MANIFEST, PROCESSED)
    except state.MigrationRefused as e:
        # Never fall through to an empty database. New code plus no state means
        # every video looks unprocessed and the next run buys all of them again.
        sys.exit(f"REFUSING TO RUN: {e}")

    all_ids = playlist_video_ids(args.playlist)
    for vid in all_ids:
        state.upsert_item(conn, vid)

    if args.reprocess:
        todo = all_ids
    else:
        def still_wanted(vid: str) -> bool:
            row = state.get_item(conn, vid)
            # Every id was upserted a moment ago, so a missing row means
            # something is wrong with the database. Skip rather than guess:
            # treating "unknown" as "not done" is what buys an episode twice.
            if row is None:
                print(f"  WARNING: {vid} has no state row; skipping this run")
                return False
            return row["status"] == state.ACTIVE

        todo = [vid for vid in all_ids if still_wanted(vid)]
    counts = state.summary(conn)
    print(f"Playlist has {len(all_ids)} videos; {len(todo)} to process. "
          f"State: {counts.get('published', 0)} published, "
          f"{counts.get('abandoned', 0)} retired, {counts.get('active', 0)} active.")

    done, stopped_early, held = 0, False, 0
    for vid in todo:
        try:
            entry = process_video(vid, args.voice_mode, args.lang, args.whisper,
                                  form_name=args.form, conn=conn)
        except QuotaExceeded as e:
            # Every remaining video would fail the same way and the credits are
            # gone regardless. Stop cleanly; the item stays active and resumes
            # next run at whatever stage it reached.
            print(f"\n  STOPPING: {e}")
            state.mark(conn, vid, "narrate", "failed", detail="quota exhausted")
            stopped_early = True
            break
        except NoTranscript as e:
            n = state.bump_attempt(conn, vid, str(e))
            state.mark(conn, vid, "transcript", "failed", detail=str(e))
            if n < state.MAX_ATTEMPTS and not state.is_stale(conn, vid):
                print(f"  HOLDING: {e} (attempt {n} of {state.MAX_ATTEMPTS})")
                held += 1
                continue
            print(f"  GIVING UP after {n} attempts: {e}")
            state.set_status(conn, vid, state.ABANDONED, str(e))
            continue
        except Exception as e:
            # No longer retired on sight. Whatever was reached is recorded, so a
            # retry resumes rather than repeats, and narration that was already
            # paid for is never bought twice. Only a run of failures retires it.
            n = state.bump_attempt(conn, vid, str(e))
            resume = state.resume_at(conn, vid)
            state.mark(conn, vid, resume or "publish", "failed", detail=str(e))
            if n >= state.MAX_ATTEMPTS or state.is_stale(conn, vid):
                print(f"  ERROR on {vid}: {e}  (attempt {n}, giving up)")
                state.set_status(conn, vid, state.ABANDONED, str(e))
            else:
                print(f"  ERROR on {vid}: {e}  (attempt {n} of {state.MAX_ATTEMPTS}, "
                      f"will resume at '{resume}')")
            continue
        if entry:
            done += 1

    counts = state.summary(conn)
    print(f"\nProcessed {done} new episode(s). Manifest: {MANIFEST.name}")
    if held:
        print(f"{held} video(s) waiting on captions; they stay queued.")
    if stopped_early:
        print(f"{len(todo) - done} video(s) left queued for the next run.")
    print(f"State: {counts.get('published', 0)} published, "
          f"{counts.get('abandoned', 0)} retired, {counts.get('active', 0)} active.")
    conn.close()
    # Exit 0 either way: build_feed and publish must still run so whatever was
    # produced actually reaches the feed.


if __name__ == "__main__":
    main()
