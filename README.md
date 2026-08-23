# Podcast Summaries

Turn long YouTube podcasts into short, NPR-style radio segments and publish them
to your own private podcast you can play in Spotify.

Add a video to a YouTube playlist, and a daily job transcribes it, has Claude
write a ~3-minute script, voices it with ElevenLabs, builds a podcast RSS feed,
and uploads everything to Cloudflare R2. Submit the feed to Spotify once and new
episodes appear automatically.

## Pipeline

```
YouTube playlist
   -> get_transcript.py   capture transcript + title (captions API / yt-dlp / Whisper)
   -> summarize.py        Claude writes the NPR-style script
   -> tts_elevenlabs.py   ElevenLabs renders the MP3 (smart voice selection)
   -> build_feed.py       regenerate podcast.xml from episodes.json
   -> publish.py          sync MP3s + cover + feed to Cloudflare R2
runner.py ties these together; run_daily.sh is the scheduled entry point.
```

## What you need

- Python 3.9+
- API keys: **ElevenLabs**, **Anthropic (Claude)**, **YouTube Data API v3** (free)
- A **Cloudflare R2** bucket with public access (r2.dev URL or a custom domain)
- A YouTube **playlist** to use as your queue (set it Unlisted, not Private —
  API keys can't read private playlists)

## Setup

```bash
git clone <your-repo-url> podcast-summaries
cd podcast-summaries
pip install -r requirements.txt
python3 setup.py          # interactive: writes .env and podcast.json
```

`setup.py` asks for your podcast name, details, keys, R2 settings, and playlist
ID. Then add a square cover image (1400–3000px) named `cover.jpg` to the folder.

Test a single video end to end:

```bash
python3 runner.py "https://www.youtube.com/watch?v=VIDEO_ID"
python3 build_feed.py
python3 publish.py        # prints your public feed URL
```

## Submit to Spotify (one time)

1. Go to Spotify for Creators (creators.spotify.com) and log in.
2. Add your podcast via **existing RSS feed**, and paste your feed URL
   (`<R2_PUBLIC_BASE>/podcast.xml`).
3. Enter the verification code Spotify emails to your owner address.

After approval, every new episode the pipeline publishes shows up automatically.

## Run with Docker (recommended)

The image is self-scheduling: it stays running and executes the pipeline once a
day on its own — no host cron or systemd needed. Your secrets and state stay on
the host (bind-mounted), never baked into the image.

```bash
git clone https://github.com/TomNerdery/PodcastSummarizer.git ~/podcast-summaries
cd ~/podcast-summaries

# Provide your config + state in this folder (see "Migrating an existing
# instance" below, or run `python3 setup.py` and add cover.jpg for a fresh start).

# Set your timezone and run time in docker-compose.yml (TZ, RUN_AT), then:
docker compose up -d --build
```

Manage it:

```bash
docker compose logs -f            # watch the scheduler + runs
docker compose exec podcast /bin/bash run_daily.sh   # trigger a run now
docker compose restart            # apply config changes
docker compose down               # stop
```

The container reads `.env`, `podcast.json`, `cover.jpg`, and writes episodes,
`episodes.json`, `processed.json`, and `podcast.xml` into the same mounted
folder, so all state persists on the host and `run.log` is right there.

- **Update the code:** `git pull` then `docker compose up -d --build`.
- **Move to another server:** copy this folder (code + state) to the new host —
  or `git clone` and copy just the git-ignored state files — then
  `docker compose up -d --build`. Because it publishes to the same R2 bucket and
  feed URL, Spotify needs no changes.

## Schedule it (Linux, without Docker)

`run_daily.sh` runs the whole chain (playlist -> episodes -> feed -> R2). Pick one:

### Option A — systemd user timer (recommended on an always-on box)

Edit the paths in `deploy/podcast-summaries.service`, then:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/podcast-summaries.service ~/.config/systemd/user/
cp deploy/podcast-summaries.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now podcast-summaries.timer
loginctl enable-linger "$USER"     # lets it run without you being logged in
```

Check it:

```bash
systemctl --user list-timers | grep podcast
systemctl --user start podcast-summaries.service   # run once now
cat run.log
```

### Option B — cron

```bash
crontab -e
# add (adjust the path):
0 6 * * * /bin/bash /home/YOUR_USER/podcast-summaries/run_daily.sh
```

## Migrating an existing instance

GitHub holds the code; your secrets and state are git-ignored and move
separately. After cloning on the new host, copy these from the old one (run on
the old machine, adjust `you@server` and paths):

```bash
rsync -av .env podcast.json cover.jpg episodes.json processed.json \
  voices.json episodes \
  you@server:~/podcast-summaries/
```

`processed.json` is what stops the new host from reprocessing the whole playlist
and re-spending API credits. Then start it (Docker or systemd). Run only one
instance at a time so episodes aren't published twice.

## Voice selection

`tts_elevenlabs.py` supports several modes (`runner.py --voice-mode`):

- `smart` (default) — Claude reads the script and picks the best-fitting voice
- `match` — keyword rules in `voices.json`
- `rotate` / `random` — cycle through your voices
- `fixed` — always one voice

Copy `voices.example.json` to `voices.json` and edit it, or run
`python3 tts_elevenlabs.py --init-config` to scaffold one from your own
ElevenLabs library. `--preview` downloads free sample clips so you can audition.

**Every mode avoids the voices used most recently** (the last 6, tracked in
`.voice_state.json`). Without that, `smart` converges hard: asked for the best
narrator for a news read, the model returns the same answer every day, because
the most broadcast-sounding voice in your library genuinely does win on merit
every time. Filtering recent picks out of the roster is what keeps the show from
sounding like one person.

### Reporter names

Each voice in `voices.json` carries a `reporter` persona name. The summary
prompt ends every script with `I'm {{REPORTER}}, for <show>.` and the pipeline
substitutes the real name once the voice is cast, so segments sound like a
person filed them.

The placeholder exists because of ordering: the script is written *before* the
narrator is chosen (the voice is picked by reading the finished script), so the
name cannot be known at writing time. Keep the reporter's implied accent and
gender consistent with the voice.

## Polling frequency, and waiting for captions

`--playlist` dedupes against `processed.json`, so running often is close to
free: a run with nothing new to do takes seconds, `playlistItems.list` costs 1
unit against YouTube's 10,000/day, and TTS spend follows episodes produced
rather than runs attempted. Hourly is a reasonable default if you add videos
through the day and want them ready the same evening.

**Running more often changes one thing, so this is handled explicitly.**
YouTube publishes captions some time after a video goes up. Poll soon enough
after adding a video and there are legitimately no captions yet. Retiring the
video at that point would lose the episode for good, which is what happens if
"failed" and "done" are the same state.

So the transcript stage is treated separately from every other failure, because
it is the only one that happens **before anything is paid for**:

- No captions yet: the video is *held*, not retired. `processed.json` keeps an
  attempt count under `waiting`, and the next run looks again. After
  `MAX_TRANSCRIPT_ATTEMPTS` (6) it gives up and retires it, so a video that
  genuinely has no captions does not get retried forever. Set `--whisper` if
  you want caption-less videos transcribed locally instead.
- Any other failure: the video is retired immediately, on purpose. Anything
  that got past the transcript may already have spent TTS credits, and a blind
  retry pays for it twice.

A video removed from the playlist drops its attempt count, so re-adding it
starts fresh. An older `processed.json` with only `ids` loads unchanged.

If you schedule this, make sure only one run can be in flight at a time
(Kubernetes: `concurrencyPolicy: Forbid`) and that a failed run is not retried
(`backoffLimit: 0`). Both exist for the same reason as the rule above.

## Segment forms

One fixed script shape is what makes a run of episodes sound like a template
with different words in it, and no amount of rewording fixes that. So
`summary-prompt.md` holds only the **spine**, the rules that never vary (length,
attribution, no invented quotes, speakable numbers, the sign-off placeholder),
and the **shape** comes from `formats.json`: a roster of segment forms such as a
cold open, a single-thread narrative, a claim tested against its best objection,
or a tight correction.

One form is chosen per episode, at random from those not used in the last three
episodes, and the choice is recorded in `episodes.json`. It is a rotation and
not a model's judgment on purpose: a model asked which shape fits best answers
the same way every day, exactly as the voice caster did before it was given a
history to avoid.

`formats.json` lives on `DATA_DIR`, so it can be edited on a running deployment
without rebuilding. Copy `formats.example.json` to start. A form may declare
`requires` (currently only `"numbers"`) to keep itself out of the rotation for
sources that lack what it needs.

To judge a change without paying for narration:

```bash
python3 runner.py <video> --dry-run --form all   # one script per form, no TTS
python3 runner.py <video> --form cold-open       # force one shape for a real run
```

`--dry-run` writes to `dry-runs/` and stops before the voice step, so it spends
Claude tokens and no ElevenLabs credits. Read them, cut the forms that do not
work, then voice a few of the survivors and listen: the output is audio, and
every defect this project has found was found by ear.

## Intro and outro music

`assemble.py` wraps each episode with a short sting at both ends, so episodes
don't run straight into one another. Assets live outside the image, in
`$DATA_DIR/assets/intro.mp3` and `outro.mp3`.

```bash
python3 make_stings.py      # generate a starter pair with ffmpeg, no API spend
```

Or cut them from your own tracks, one intro/outro pair per song:

```bash
python3 make_stings.py --from-music ~/my-music --seconds 5
```

The intro comes off the START of each track and the outro off the END, both
with fades. `assemble.py` then rotates through the pairs, one tune per episode,
keeping the intro and outro of any single episode from the same track.
[Pixabay's music library](https://pixabay.com/music/) is CC0, free for
commercial use with no attribution.

Everything is loudness-matched (voice to -16 LUFS, music to -20) so the sting
cues the transition instead of blasting over the narrator. Segments are joined
end to end with a short silence, never crossfaded: any overlap has to fade one
side down, and the side that loses is always the voice's first or last words.

If ffmpeg is missing or the assets aren't there, the bare narration is used: a
missing sting never costs you an episode.

The narration-only audio is kept beside each episode as `<name>.narration.mp3`
(never uploaded). Assembly is free to redo and TTS is not, so changing the
stings later means re-rendering for nothing instead of paying to re-voice:

```bash
python3 revoice.py --all          # reassemble every episode, no API spend
```

## Source audio clips (off by default)

Set `CLIPS_ENABLED=1` and episodes can include short excerpts of the original
speaker's audio, handed off to by the narrator like a real news segment.

How it works: transcripts are captured with per-line timings, the summarizer
places `[[CLIP 412.5-424.0]]` markers in the script, `yt-dlp` fetches only those
seconds, the cut is snapped to the nearest silence so it never starts mid-word,
and the pieces are joined narration / clip / narration. Running cost is roughly
unchanged, because every second of clip is a second of narration you no longer
pay to synthesise.

Enforced in code, not left to the prompt:

- 15 seconds maximum per clip, 45 seconds maximum per episode, 3 clips maximum
- clips are chosen to be *illustrative of the argument*, explicitly not the
  single most quotable line
- the excerpt timestamps are credited in the episode's show notes
- clip audio is never uploaded as a standalone file, only inside the episode
- if any clip fails, the script is rewritten without clips, so a hand-off line
  never plays with nothing after it

**Before you turn this on:** publishing an AI paraphrase and publishing someone
else's actual audio are different positions. Attribution is good practice but is
not by itself a copyright defence, and downloading the audio conflicts with
YouTube's Terms of Service regardless of copyright. A private feed is the safest
posture. This is not legal advice; the default is off so the choice is yours.

## Recovering lost episodes

If a run dies between paying for the audio and recording the episode, the MP3 is
on disk but absent from the feed. `repair_manifest.py` finds those, matches them
back to their source video by title, and files them:

```bash
python3 repair_manifest.py --dry-run          # show what it would recover
python3 repair_manifest.py --wrap             # recover, and add stings to bare episodes
python3 build_feed.py && python3 publish.py --force
```

Use `--force` on publish after `--wrap`, because publish skips MP3s already in
the bucket and wrapping rewrites them.

## Files

| File | Purpose |
|------|---------|
| `setup.py` | First-run wizard; writes `.env` + `podcast.json` |
| `runner.py` | Orchestrator; single video or `--playlist` |
| `get_transcript.py` | Transcript + metadata capture |
| `summarize.py` | Claude summarization (spine in `summary-prompt.md`) |
| `formats.py` | Segment forms: picks the shape of each episode and rotates it |
| `tts_elevenlabs.py` | ElevenLabs TTS + voice selection |
| `assemble.py` | Wraps narration with the intro/outro stings (ffmpeg) |
| `make_stings.py` | Generates a starter pair of stings |
| `build_feed.py` | Generates `podcast.xml` |
| `publish.py` | Uploads to Cloudflare R2 |
| `repair_manifest.py` | Recovers episodes whose audio exists but never published |
| `revoice.py` | Re-renders episodes after a change to the stings or assembly |
| `clips.py` | Cuts short excerpts of the source audio (off by default) |
| `run_daily.sh` | Scheduled entry point |
| `voices.example.json` | Template voice roster + reporter names |
| `formats.example.json` | Template segment forms + show sign-on lines |
| `deploy/` | systemd unit + timer templates |

## Notes

- `.env`, `podcast.json`, `voices.json`, `formats.json`, audio, and the feed are git-ignored.
- Transcripts need direct YouTube access, so run this where the network is open
  (your always-on machine), not inside a restricted sandbox.
- The `r2.dev` public URL is rate-limited; use a custom domain for production.
