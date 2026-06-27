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

## Schedule it (Linux)

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

## Voice selection

`tts_elevenlabs.py` supports several modes (`runner.py --voice-mode`):

- `smart` (default) — Claude reads the script and picks the best-fitting voice
- `match` — keyword rules in `voices.json`
- `rotate` / `random` — cycle through your voices
- `fixed` — always one voice

Run `python3 tts_elevenlabs.py --init-config` to scaffold `voices.json` from your
ElevenLabs library, and `--preview` to download free sample clips.

## Files

| File | Purpose |
|------|---------|
| `setup.py` | First-run wizard; writes `.env` + `podcast.json` |
| `runner.py` | Orchestrator; single video or `--playlist` |
| `get_transcript.py` | Transcript + metadata capture |
| `summarize.py` | Claude summarization (prompt in `summary-prompt.md`) |
| `tts_elevenlabs.py` | ElevenLabs TTS + voice selection |
| `build_feed.py` | Generates `podcast.xml` |
| `publish.py` | Uploads to Cloudflare R2 |
| `run_daily.sh` | Scheduled entry point |
| `deploy/` | systemd unit + timer templates |

## Notes

- `.env`, `podcast.json`, `voices.json`, audio, and the feed are git-ignored.
- Transcripts need direct YouTube access, so run this where the network is open
  (your always-on machine), not inside a restricted sandbox.
- The `r2.dev` public URL is rate-limited; use a custom domain for production.
