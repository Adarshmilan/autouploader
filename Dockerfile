# Splitter: YouTube link -> AI highlights -> vertical clips -> YouTube Shorts + Instagram Reels
FROM python:3.14-slim

# ffmpeg: cutting/encoding clips (Debian's build includes libx264)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js: yt-dlp needs a recent one (Debian's v20 is too old) to solve YouTube's JS challenges
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Install dependencies first (cached unless pyproject/uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Everything that must survive restarts (clips, uploads.json, YouTube login + quota) goes here.
# Mount a volume on it.
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data

ENTRYPOINT ["/app/.venv/bin/splitter"]
