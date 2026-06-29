FROM python:3.12-slim

# ffmpeg: used by yt-dlp / optional Whisper fallback. tzdata: lets RUN_AT honor TZ.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code (state/secrets are excluded via .dockerignore and
# bind-mounted at runtime).
COPY . .
RUN chmod +x run_daily.sh docker/run_scheduler.sh

ENV RUN_AT=06:00

# Self-scheduling: the container stays up and runs the pipeline daily.
CMD ["/bin/bash", "/app/docker/run_scheduler.sh"]
