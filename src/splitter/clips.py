"""Step 3: download ONLY the hyped parts and turn each into a vertical 1080x1920 clip.

yt-dlp downloads just the needed seconds (exact cut points), then FFmpeg turns that piece
into a 1080x1920 H.264 + AAC clip with a blurred background.
"""

import glob
import os
import subprocess
import time

import yt_dlp
from yt_dlp.utils import download_range_func

from splitter import config

WIDTH, HEIGHT = 1080, 1920
ATTEMPTS = 3

# Prefer plain https (DASH) streams: cutting them works with every FFmpeg version.
# (HLS/m3u8 streams + exact cuts give a clip with NO video on FFmpeg 7, e.g. in Docker.)
# 1080p is plenty for a 1080-wide vertical clip.
FORMAT = (
    "bestvideo[height<=1080][protocol=https]+bestaudio[ext=m4a][protocol=https]"
    "/bestvideo[height<=1080][protocol=https]+bestaudio[protocol=https]"
    "/bestvideo[height<=1080]+bestaudio"
    "/best[height<=1080]/best"
)

# Blurred background: a zoomed + blurred copy fills the 9:16 frame, the full video sits in the middle.
FILTER = (
    f"[0:v]split=2[bg][fg];"
    f"[bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},boxblur=20:2[bg];"
    f"[fg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fg];"
    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
)

# H.264 encoders to try, best first. Docker/Ubuntu/Debian FFmpeg has libx264.
# Fedora's FFmpeg doesn't (patents), so locally it falls back to the NVIDIA GPU or OpenH264.
ENCODERS = [
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-b:v", "0"],
    ["-c:v", "libopenh264", "-b:v", "6M", "-maxrate", "8M"],
]
_working_encoder = None  # remembered after the first success


def download_section(url, start, end, out_dir, name):
    """Download just start..end of the video (no full download). Returns the file path."""
    options = {
        **config.YTDLP_BASE,
        "format": FORMAT,
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,  # exact cut points instead of the nearest keyframe
        "paths": {"home": out_dir},
        "outtmpl": f"{name}.%(ext)s",
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    path = os.path.join(out_dir, f"{name}.mp4")
    if not os.path.exists(path):
        raise RuntimeError(f"yt-dlp didn't produce {path}")
    return path


def _stream_types(path):
    """Which kinds of tracks a file has, e.g. {"video", "audio"}."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return set(result.stdout.split())


def _remove_pieces(out_dir, name):
    """Delete raw_01.mp4 and any half-downloaded parts (raw_01.f140.m4a, .part, ...)."""
    for leftover in glob.glob(os.path.join(glob.escape(out_dir), f"{name}.*")):
        os.remove(leftover)


def make_vertical(src, dst):
    """Re-encode to 1080x1920 H.264 + AAC (the format Instagram and YouTube both accept)."""
    global _working_encoder
    errors = []
    for encoder in [_working_encoder] if _working_encoder else ENCODERS:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
            "-i", src,
            "-filter_complex", FILTER,
            "-map", "[v]", "-map", "0:a?",
            *encoder, "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart",
            dst,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            _working_encoder = encoder
            return
        errors.append(f"{encoder[1]}: {result.stderr.strip()[-400:]}")

    raise RuntimeError("FFmpeg couldn't encode H.264 with any encoder:\n" + "\n".join(errors))


def _codecs(path):
    """{"video": "h264", "audio": "aac"} (missing kinds are left out)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    found = {}
    for line in result.stdout.split():
        name, _, kind = line.partition(",")
        found.setdefault(kind, name)
    return found


def make_compatible(src, dst):
    """Keep the picture as it is, only re-encode to H.264 + AAC MP4 (what Instagram and YouTube accept)."""
    global _working_encoder
    errors = []
    for encoder in [_working_encoder] if _working_encoder else ENCODERS:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", src,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=trunc(min(iw\\,1920)/2)*2:-2,format=yuv420p",
            *encoder, "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", dst,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            _working_encoder = encoder
            return
        errors.append(f"{encoder[1]}: {result.stderr.strip()[-400:]}")
    raise RuntimeError("FFmpeg couldn't convert the video:\n" + "\n".join(errors))


def prepare_upload(src, dst, vertical=False):
    """Your own video -> an uploadable .mp4 at dst. Already-fine MP4s are just moved (no quality loss)."""
    tmp = dst + ".tmp.mp4"
    try:
        codecs = _codecs(src)
        if "video" not in codecs:
            raise RuntimeError("that file has no video track (or isn't a video)")
        if vertical:
            print("   making it vertical 1080x1920...")
            make_vertical(src, tmp)
        elif (codecs.get("video") == "h264" and codecs.get("audio") in ("aac", None)
              and src.lower().endswith((".mp4", ".m4v"))):
            os.replace(src, dst)
            return
        else:
            print(f"   converting {codecs.get('video')}/{codecs.get('audio', 'no audio')} to H.264/AAC...")
            make_compatible(src, tmp)
        os.replace(tmp, dst)
    finally:
        for leftover in (tmp, src):
            if os.path.exists(leftover):
                os.remove(leftover)


def make_clips(url, moments, out_dir, video_duration=0):
    """Create clip_01.mp4, clip_02.mp4, ... in out_dir. Clips that already exist are reused."""
    paths = []
    for i, m in enumerate(moments, start=1):
        clip = os.path.join(out_dir, f"clip_{i:02d}.mp4")
        if os.path.exists(clip):
            paths.append(clip)
            continue

        start = max(0.0, m["start"] - config.CLIP_PADDING)
        end = m["end"] + config.CLIP_PADDING
        if video_duration:
            end = min(end, video_duration)
        print(f"   clip {i}: {start:.1f}s -> {end:.1f}s  ({m['title']})")

        name, tmp = f"raw_{i:02d}", clip + ".tmp.mp4"
        for attempt in range(1, ATTEMPTS + 1):
            _remove_pieces(out_dir, name)  # every try starts clean (a broken leftover once gave a silent clip)
            try:
                raw = download_section(url, start, end, out_dir, name)
                missing = {"video", "audio"} - _stream_types(raw)
                if missing:
                    raise RuntimeError(f"downloaded part has no {' or '.join(sorted(missing))}")
                make_vertical(raw, tmp)
                if {"video", "audio"} - _stream_types(tmp):
                    raise RuntimeError("finished clip is missing video or audio")
                break
            except (RuntimeError, yt_dlp.utils.DownloadError) as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                if attempt == ATTEMPTS:
                    raise
                print(f"   hiccup ({str(e).strip().splitlines()[-1][:120]}), retrying ({attempt}/{ATTEMPTS})...")
                time.sleep(5 * attempt)
            finally:
                _remove_pieces(out_dir, name)  # the uncropped piece isn't needed once the clip is made

        os.replace(tmp, clip)  # only a finished clip gets the real name
        paths.append(clip)
    return paths
