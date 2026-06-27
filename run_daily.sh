#!/bin/bash
# Daily pipeline run for "The Gist": new playlist videos -> episodes -> feed -> R2.
# Scheduled by com.thegist.daily.plist (launchd). Logs to run.log.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

# cron/launchd/systemd start with a minimal PATH; add common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

LOG="run.log"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] ===== starting daily run =====" >> "$LOG"

# Read the playlist to poll from .env (PLAYLIST_ID=PLxxxx).
PLAYLIST_ID="$(grep -E '^PLAYLIST_ID=' .env 2>/dev/null | cut -d= -f2- | tr -d '\"'"'"' ')"
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
