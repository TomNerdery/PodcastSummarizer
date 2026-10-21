FROM python:3.12-slim

# ffmpeg: used by yt-dlp / optional Whisper fallback. tzdata: lets RUN_AT honor TZ.
# curl: fetches the Claude Code installer below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Claude Code, for the OPT-IN subscription backend in claude_call.py. It is not
# used at all unless CLAUDE_CODE_OAUTH_TOKEN is set; with ANTHROPIC_API_KEY the
# pipeline calls the API directly, as it always has.
#
# Installed with Anthropic's own native installer, at a pinned version so a
# rebuild cannot quietly change the CLI. The installer puts the binary under
# root's home; it is moved, unmodified, to /usr/local/bin so it is on PATH for
# any user. Auto-update is off because the version is pinned here instead.
# To upgrade: change CLAUDE_CODE_VERSION and rebuild.
ARG CLAUDE_CODE_VERSION=2.1.304
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && mv "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /root/.local/bin/claude /root/.local/share/claude /root/.claude \
              /root/.claude.json /root/.cache /root/.npm \
    && claude --version | grep -F "${CLAUDE_CODE_VERSION}"
ENV DISABLE_AUTOUPDATER=1

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
