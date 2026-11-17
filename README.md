# Splitter

Turn a long YouTube video into vertical Shorts / Reels and post them to **YouTube** and **Instagram** — from a simple web page.

- **Paste a YouTube link** → transcript → AI picks the most hyped moments → each one is cut into a 1080×1920 clip → you watch them, edit the text, pick YouTube / Instagram (both by default) and press **Post**.
- **Or drop your own video** → skips the AI part and posts it directly (converted to H.264/AAC if needed, optionally made vertical).
- Shows YouTube uploads left today + countdown to the reset, Instagram's 24 h limit, and clips waiting to upload. Clips over the daily cap upload automatically later.
- Keeps only the newest 3 videos on disk (`KEEP_VIDEOS`).

## Setup

Needs Python 3.14+, [uv](https://docs.astral.sh/uv/), FFmpeg and Node.js (or just Docker).

```bash
cp .env.example .env          # then fill it in (at least OPENROUTER_API_KEY and APP_PASSWORD)
uv sync
```

**YouTube login (once):** put your Google OAuth client file (Desktop app) at `client_secret.json`, then

```bash
uv run splitter --login       # opens a browser, saves youtube_token.json
```

**Instagram:** set `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` in `.env` (Business/Creator account).

## Run

```bash
uv run splitter --serve       # web page at http://localhost:8080  (log in with APP_USER / APP_PASSWORD)
```

With Docker (keeps running after reboots; put `youtube_token.json` and `cookies.txt` in `./data`):

```bash
docker compose up -d --build
docker compose logs -f
```

Command line, without the web page:

```bash
uv run splitter "https://www.youtube.com/watch?v=..."   # makes clips and uploads straight away
uv run splitter --pending                               # upload clips that were waiting
```

## Project layout

```
src/splitter/
  main.py              pipeline: link -> transcript -> highlights -> clips -> upload; review, cleanup
  transcript.py        video info + captions (yt-dlp)
  highlights.py        AI picks the moments (OpenRouter)
  clips.py             download just those parts, make vertical clips (FFmpeg)
  youtube_upload.py    YouTube Shorts upload + daily cap
  instagram_upload.py  Instagram Reels upload
  server.py            web server + JSON API + background worker
  config.py            settings from .env
  web/
    index.html         page layout
    style.css          look & colours
    js/app.js          helpers, server calls, auto-refresh
    js/forms.js        "YouTube link" and "Upload my video" tabs
    js/render.js       quota cards, activity, review clips, history
```

## Notes

- **YouTube videos are private?** Google forces uploads from unverified API projects to private. Set the OAuth consent screen to *In production* and submit the *YouTube API Services Audit* form; after approval `YOUTUBE_PRIVACY=public` works.
- **Never commit** `.env`, `client_secret.json`, `youtube_token.json` or `cookies.txt` — they are in `.gitignore`.
