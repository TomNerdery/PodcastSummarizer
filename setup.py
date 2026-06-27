#!/usr/bin/env python3
"""
First-run setup wizard.

Walks you through entering your API keys, podcast details, and Cloudflare R2
settings, then writes two files:
    .env           your secrets + playlist id  (never commit this)
    podcast.json   your show's public details (name, description, feed URLs)

Re-run any time to update settings. Secret values are entered hidden (no echo).

    python3 setup.py
"""

import getpass
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
SHOW_PATH = HERE / "podcast.json"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_secret(prompt: str, current: str = "") -> str:
    hint = " (press Enter to keep existing)" if current else ""
    val = getpass.getpass(f"{prompt}{hint}: ").strip()
    return val or current


def load_existing_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def main() -> None:
    print("\n=== Podcast Summaries — setup ===\n")
    print("This collects your settings and writes .env and podcast.json.")
    print("Get API keys from: ElevenLabs, console.anthropic.com, Google Cloud "
          "(YouTube Data API v3), and your Cloudflare R2 dashboard.\n")

    existing = load_existing_env()

    # --- Show details (public) ---
    print("--- Your podcast ---")
    title = ask("Podcast name", "My Podcast")
    description = ask("One-line description",
                      "Long-form podcasts distilled into short radio segments.")
    author = ask("Your name (shown as author)")
    email = ask("Owner email (Spotify verifies ownership here)")

    # --- Cloudflare R2 public URL drives the feed URLs ---
    print("\n--- Hosting (Cloudflare R2) ---")
    public_base = ask("R2 public base URL (e.g. https://pub-xxxx.r2.dev)",
                      existing.get("R2_PUBLIC_BASE", "")).rstrip("/")

    show = {
        "title": title,
        "description": description,
        "author": author,
        "email": email,
        "language": "en-us",
        "category": "News",
        "explicit": False,
        "site_url": public_base,
        "base_url": f"{public_base}/episodes",
        "image_url": f"{public_base}/cover.jpg",
    }

    # --- Secrets + ids ---
    print("\n--- API keys (input hidden) ---")
    eleven = ask_secret("ELEVENLABS_API_KEY", existing.get("ELEVENLABS_API_KEY", ""))
    anthropic = ask_secret("ANTHROPIC_API_KEY", existing.get("ANTHROPIC_API_KEY", ""))
    youtube = ask_secret("YOUTUBE_API_KEY", existing.get("YOUTUBE_API_KEY", ""))

    print("\n--- Cloudflare R2 credentials ---")
    r2_account = ask("R2_ACCOUNT_ID", existing.get("R2_ACCOUNT_ID", ""))
    r2_key = ask_secret("R2_ACCESS_KEY_ID", existing.get("R2_ACCESS_KEY_ID", ""))
    r2_secret = ask_secret("R2_SECRET_ACCESS_KEY", existing.get("R2_SECRET_ACCESS_KEY", ""))
    r2_bucket = ask("R2_BUCKET", existing.get("R2_BUCKET", "podcast"))

    print("\n--- YouTube queue ---")
    playlist = ask("PLAYLIST_ID (the list= value of your queue playlist)",
                   existing.get("PLAYLIST_ID", ""))

    env_lines = [
        "# Secrets and IDs — do NOT commit this file.",
        f"ELEVENLABS_API_KEY={eleven}",
        f"ANTHROPIC_API_KEY={anthropic}",
        f"YOUTUBE_API_KEY={youtube}",
        f"R2_ACCOUNT_ID={r2_account}",
        f"R2_ACCESS_KEY_ID={r2_key}",
        f"R2_SECRET_ACCESS_KEY={r2_secret}",
        f"R2_BUCKET={r2_bucket}",
        f"R2_PUBLIC_BASE={public_base}",
        f"PLAYLIST_ID={playlist}",
        "",
    ]
    ENV_PATH.write_text("\n".join(env_lines), encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)  # owner read/write only
    except OSError:
        pass
    SHOW_PATH.write_text(json.dumps(show, indent=2) + "\n", encoding="utf-8")

    print(f"\nWrote {ENV_PATH.name} (permissions 600) and {SHOW_PATH.name}.")
    print("\nNext steps:")
    print("  1. Put a square cover image (1400-3000px) named cover.jpg in this folder.")
    print("  2. pip install -r requirements.txt")
    print("  3. python3 runner.py --playlist $PLAYLIST_ID   (or a single video URL)")
    print("  4. python3 build_feed.py && python3 publish.py")
    print("  5. Optionally run tts_elevenlabs.py --init-config to enable voice rotation.\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nSetup cancelled.")
