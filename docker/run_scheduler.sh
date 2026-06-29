#!/bin/bash
# In-container scheduler: runs run_daily.sh once per day at $RUN_AT (container
# local time, set via the TZ env var). No host cron needed — keep the container
# running (restart: unless-stopped) and it fires every day on its own.
set -u

RUN_AT="${RUN_AT:-06:00}"
APP_DIR="/app"

echo "[scheduler] started $(date) — daily run at ${RUN_AT} (TZ=${TZ:-UTC})"

# Optional: run immediately on container start (set RUN_ON_START=1).
if [ "${RUN_ON_START:-0}" = "1" ]; then
    echo "[scheduler] RUN_ON_START set — running now"
    /bin/bash "${APP_DIR}/run_daily.sh" || echo "[scheduler] run exited non-zero"
fi

while true; do
    now=$(date +%s)
    target=$(date -d "today ${RUN_AT}" +%s)
    [ "$target" -le "$now" ] && target=$(date -d "tomorrow ${RUN_AT}" +%s)
    sleep_secs=$((target - now))
    echo "[scheduler] next run at $(date -d "@${target}") (sleeping ${sleep_secs}s)"
    sleep "$sleep_secs"
    echo "[scheduler] ===== triggering run_daily.sh at $(date) ====="
    /bin/bash "${APP_DIR}/run_daily.sh" || echo "[scheduler] run exited non-zero"
done
