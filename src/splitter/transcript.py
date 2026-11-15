"""Step 1: get the video's info and its YouTube transcript (json3, or vtt if json3 isn't offered)."""

import glob
import os

import yt_dlp

from splitter import config


def get_video_info(url):
    """Ask YouTube for the video's id, title, duration — without downloading anything."""
    with yt_dlp.YoutubeDL(config.YTDLP_BASE) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info["id"],
        "title": info.get("title", ""),
        "duration": info.get("duration") or 0,
        "url": info.get("webpage_url", url),
    }


def find_transcript(out_dir):
    """Return an already-downloaded transcript in out_dir (json3 preferred), or None."""
    for ext in ("json3", "vtt"):
        files = sorted(glob.glob(os.path.join(out_dir, f"transcript.*.{ext}")))
        if files:
            return files[0]
    return None


def get_transcript(url, out_dir):
    """Download the transcript into out_dir. Returns its path, or None if the video has no captions."""
    existing = find_transcript(out_dir)
    if existing:
        return existing

    options = {
        **config.YTDLP_BASE,
        "skip_download": True,          # only the transcript, not the video
        "writesubtitles": True,         # creator-uploaded subtitles (preferred when they exist)
        "writeautomaticsub": True,      # YouTube's auto-generated captions as backup
        "subtitleslangs": config.TRANSCRIPT_LANGUAGES,
        "subtitlesformat": "json3/vtt/best",
        "paths": {"home": out_dir},
        "outtmpl": "transcript.%(ext)s",  # -> transcript.en.json3
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    return find_transcript(out_dir)


if __name__ == "__main__":
    link = input("Enter YouTube URL: ")
    folder = os.path.join(config.OUTPUT_DIR, get_video_info(link)["id"])
    os.makedirs(folder, exist_ok=True)
    print(get_transcript(link, folder) or "This video has no captions.")
