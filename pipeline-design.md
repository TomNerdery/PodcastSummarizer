# Podcast Summaries — Pipeline Design

Goal: drop a long YouTube podcast into a queue and automatically get a short, NPR-style radio piece published to a personal podcast you can play in Spotify while driving.

The flow has five stages: **Ingest → Transcribe → Summarize → Voice → Publish.** Below is the recommended build, the alternatives at each stage, and what it costs.

---

## 1. Ingest — how videos get queued

**Recommended: a dedicated YouTube playlist.** Make an unlisted playlist called something like "To Summarize." Adding a video to it from any device (phone or desktop) is two taps, and YouTube's Data API lets an automated job read the playlist's contents on a schedule. No extension to build or maintain.

A scheduled job polls the playlist every few hours, compares against a list of already-processed video IDs, and processes anything new.

Alternatives:
- **Chrome extension / bookmarklet** — more flexible (queue from the page you're watching) but it's a real piece of software to build and keep working against YouTube changes. The playlist gets you 95% of the benefit for none of the maintenance.
- **"Watch Later" or a Saved playlist** — works the same way technically, but a dedicated playlist keeps the queue clean.

## 2. Transcribe — get the words

**Primary: `youtube-transcript-api`** (Python). When YouTube publishes captions (DOAC always does), this pulls them in well under a second, free. *Note: it must run somewhere that can reach YouTube directly — your own machine or a cloud server. It does not work inside this Cowork sandbox, whose network blocks YouTube.*

Fallbacks, in order:
1. **`yt-dlp`** to grab the auto-generated caption track if the API is rate-limited.
2. **Whisper** (OpenAI API or local `faster-whisper`) on the downloaded audio when there are no captions at all. Slower and, via API, costs a few cents per hour of audio.

## 3. Summarize — long transcript to radio script

A single Claude API call with a fixed system prompt turns the transcript into the ~470-word NPR-style essay (the prompt I used for the Grantham episode is saved as `summary-prompt.md`). Feed it the transcript plus the video title and description; the description's chapter markers are a useful outline. Cost is roughly a cent or two per episode.

## 4. Voice — script to audio

This is the one stage with a real quality/cost tradeoff.

| Option | Quality | Cost | Notes |
|---|---|---|---|
| **ElevenLabs** | Best, very natural | ~$5–22/mo | Most "real radio" sounding; one API call. **Recommended for the final product.** |
| **OpenAI TTS** | Very good | ~$0.015/1k chars (pennies/episode) | Simple API, several voices. Great value. |
| **Piper (local)** | Decent, clearly synthetic | Free | Runs offline once the voice model is downloaded; good for a zero-cost setup. |
| **espeak-ng (local)** | Robotic | Free | What today's demo MP3 uses, because the model hosts for the better engines were blocked in this sandbox. Fine as a placeholder, not for daily listening. |

**Recommendation:** OpenAI TTS for the price/quality balance, or ElevenLabs if you want it to sound truly broadcast-grade. Either needs an API key you provide.

## 5. Publish — get it into Spotify

Spotify does not accept uploads directly; it ingests an **RSS feed**. Two paths:

**A. Easiest — Spotify for Creators** (formerly Anchor; free). Create a show, and each run uploads the new MP3 via its API (or you drop the file in). Spotify hosts the audio, generates the RSS feed, and distributes to Spotify automatically. Lowest effort, no servers.

**B. Full DIY** — generate your own `podcast.xml` RSS file, host the MP3s on cheap object storage (Cloudflare R2, AWS S3, or even GitHub Pages), and submit that RSS URL once to Spotify for Podcasters. You own everything; Spotify re-checks the feed periodically and pulls new episodes. More control, ~$0–1/mo, a bit more plumbing.

**Recommendation:** start with **Spotify for Creators** to get listening fast; move to DIY only if you want full ownership of the feed.

---

## Putting it together

A single scheduled script ties it all together:

```
every few hours:
  new = playlist_items() - already_processed
  for video in new:
      transcript = get_transcript(video)
      script     = claude_summarize(transcript, title, description)
      mp3        = tts(script)                 # OpenAI / ElevenLabs / Piper
      publish(mp3, title)                      # upload + RSS update
      mark_processed(video)
```

Where it runs:
- **A small always-on host** (a cheap VPS, a home machine, or a serverless cron) is the robust home for this, because transcription needs direct YouTube access.
- **This Cowork app can run the summarize + voice + publish steps on a schedule** and is great for the demo and for tuning the script style — but it can't fetch YouTube transcripts itself, so the ingest/transcribe step needs to happen on a machine with open network access and hand the text to the rest.

## What you need to provide to go live
1. The unlisted **YouTube playlist** (and a YouTube Data API key — free).
2. A **TTS API key** (OpenAI or ElevenLabs) for good audio.
3. A **Spotify for Creators** account (free) — or storage + a submitted RSS feed for the DIY route.

Once those exist, I can wire up the script and a schedule.
