# Deploying to your own server (Docker)

A step-by-step checklist to run this pipeline on an always-on Linux server using
Docker. Do the steps in order.

## 1. Push your code to GitHub (from your dev machine)

```bash
git push        # first time ever: git push -u origin main
```

## 2. Install Docker on the server (if needed)

```bash
docker --version || curl -fsSL https://get.docker.com | sh
```

## 3. Clone the repo on the server

```bash
git clone https://github.com/TomNerdery/PodcastSummarizer.git ~/podcast-summaries
cd ~/podcast-summaries
```

## 4. Copy your private settings onto the server

Your keys, show config, cover art, and episode history are git-ignored, so copy
them separately. Run this **on your dev machine** (adjust `you@server`):

```bash
rsync -av .env podcast.json cover.jpg episodes.json processed.json voices.json episodes \
  you@server:~/podcast-summaries/
```

`processed.json` prevents the server from reprocessing the whole playlist and
re-spending API credits. (For a brand-new instance instead of a migration, run
`python3 setup.py` and add a `cover.jpg` rather than copying these.)

### Optional: use your Claude subscription instead of an API key

Off by default. To turn it on, edit `.env` **on the server**:

1. On your own machine, run `claude setup-token` and copy the token it prints.
2. Add `CLAUDE_CODE_OAUTH_TOKEN=<your token>` to `.env`.
3. Delete the `ANTHROPIC_API_KEY=` line. With both set, the pipeline refuses to
   start, because the API key would win and you would still pay per token.

The image already includes Claude Code, which this mode needs. Use **your own**
plan and token, never someone else's. The token lasts one year, usage counts
against your plan's limits, and Anthropic says plan limits assume ordinary
individual use, so heavy automated use should stay on an API key. Details and
the link to Anthropic's terms: README, "Use your Claude subscription instead of
an API key".

## 5. Set your timezone

Edit `docker-compose.yml` and change `TZ=Etc/UTC` to your zone (e.g.
`America/Chicago`) so `RUN_AT` (default 06:00) fires at your local time.

## 6. Build and start

```bash
docker compose up -d --build
docker compose logs -f            # watch it; shows when the next run is scheduled
```

Prove the whole chain works immediately:

```bash
docker compose exec podcast /bin/bash run_daily.sh
```

## 7. Run only one instance

Turn off any old scheduler so episodes aren't published twice.

- macOS launchd: `launchctl bootout gui/$(id -u)/<your.job.label>`
- systemd timer: `systemctl --user disable --now podcast-summaries.timer`

## Everyday operations

| Task | Command |
|------|---------|
| Update code | `git pull && docker compose up -d --build` |
| Run now | `docker compose exec podcast /bin/bash run_daily.sh` |
| View logs | `docker compose logs -f`  (or `cat run.log`) |
| Restart (apply config) | `docker compose restart` |
| Stop | `docker compose down` |
| Move to a new server | copy this folder (code + state) there, then `docker compose up -d --build` |

Because the pipeline publishes to the same Cloudflare R2 bucket and feed URL,
Spotify needs no changes when you move or rebuild.
