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
| `summarize.py` | Claude summarization (prompt in `summary-prompt.md`) |
| `tts_elevenlabs.py` | ElevenLabs TTS + voice selection |
| `assemble.py` | Wraps narration with the intro/outro stings (ffmpeg) |
| `make_stings.py` | Generates a starter pair of stings |
| `build_feed.py` | Generates `podcast.xml` |
| `publish.py` | Uploads to Cloudflare R2 |
| `repair_manifest.py` | Recovers episodes whose audio exists but never published |
| `revoice.py` | Re-renders episodes after a change to the stings or assembly |
| `run_daily.sh` | Scheduled entry point |
| `voices.example.json` | Template voice roster + reporter names |
| `deploy/` | systemd unit + timer templates |

## Notes

- `.env`, `podcast.json`, `voices.json`, audio, and the feed are git-ignored.
- Transcripts need direct YouTube access, so run this where the network is open
  (your always-on machine), not inside a restricted sandbox.
- The `r2.dev` public URL is rate-limited; use a custom domain for production.
