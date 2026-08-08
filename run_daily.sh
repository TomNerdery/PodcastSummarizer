#!/bin/bash
# Daily pipeline run for "The Gist": new playlist videos -> episodes -> feed -> R2.
# The scheduled entry point (Docker, systemd timer, cron, or a k8s CronJob).
# Logs to run.log.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

# cron/launchd/systemd start with a minimal PATH; add common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

LOG="run.log"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] ===== starting daily run =====" >> "$LOG"

# Playlist to poll: env var wins (k8s Secret), else fall back to .env (local Docker).
PLAYLIST_ID="${PLAYLIST_ID:-$(grep -E '^PLAYLIST_ID=' .env 2>/dev/null | cut -d= -f2- | tr -d '\"'"'"' ')}"
if [ -z "${PLAYLIST_ID:-}" ]; then
  echo "[$(ts)] ERROR: PLAYLIST_ID not set in .env — aborting." >> "$LOG"
  exit 1
fi

{
  echo "--- runner (playlist $PLAYLIST_ID) ---"
  python3 runner.py --playlist "$PLAYLIST_ID" && \
  echo "--- build_feed ---" && python3 build_feed.py && \
  echo "--- publish ---"   && python3 publish.py
} >> "$LOG" 2>&1
status=$?

echo "[$(ts)] ===== finished (exit $status) =====" >> "$LOG"
exit $status
