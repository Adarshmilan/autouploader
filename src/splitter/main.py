"""The whole pipeline: link -> transcript -> AI highlights -> clips -> YouTube Shorts + Instagram Reels.

Usage:
    uv run splitter                 # asks for a link
    uv run splitter <youtube-url>   # link as argument
    uv run splitter --pending       # only upload clips that are still waiting (e.g. after YouTube's daily cap)
    uv run splitter --serve         # server mode: web page to submit links + automatic pending uploads
    uv run splitter --login         # one-time YouTube login (on a PC with a browser), then copy the token to the server

Every run first uploads clips still waiting from earlier runs, then handles the new link.
Progress is saved in output/<video-id>/uploads.json, so re-running never re-posts a clip.
"""

import glob
import json
import os
import sys
import threading
import time
import traceback

from splitter import config
from splitter.clips import make_clips
from splitter.highlights import find_highlights, fmt
from splitter.transcript import get_transcript, get_video_info

MAX_ATTEMPTS = 3  # a clip that fails this many times on a platform is not retried again


# ---------- saved state (uploads.json) ----------

def load_state(video_dir):
    path = os.path.join(video_dir, "uploads.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(video_dir, state):
    """Write to a temp file first, then swap it in: the web page never reads a half-written file."""
    path = os.path.join(video_dir, "uploads.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: someone is reading it right now
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


# approve/discard (web requests) and cleanup (worker) must not edit the same uploads.json at once
state_lock = threading.RLock()


# Per-platform status of a clip:
#   review  -> made by the web page, waiting for you to press "Post" (never uploaded automatically)
#   pending -> approved, will be uploaded (now, or later when YouTube's daily cap resets)
#   error   -> failed, retried up to MAX_ATTEMPTS times
#   done    -> posted
#   skipped -> you didn't pick this clip / platform
UPLOADABLE = ("pending", "error")


def needs_upload(clip, platform):
    entry = clip[platform]
    return entry["status"] in UPLOADABLE and entry.get("attempts", 0) < MAX_ATTEMPTS


def in_review(state):
    return any(c[p]["status"] == "review" for c in state["clips"] for p in ("youtube", "instagram"))


# ---------- uploading ----------

class Uploader:
    """Uploads clips to both platforms. Remembers if YouTube's daily cap was hit during this run."""

    def __init__(self):
        self.youtube_blocked = None  # reason string once YouTube can't take more today
        self.warned = set()

    def _warn_once(self, key, message):
        if key not in self.warned:
            print(f"   ! {message}")
            self.warned.add(key)

    def upload_youtube(self, clip, video_dir):
        from splitter import youtube_upload  # imported here so the app runs without Google libs set up

        if not config.youtube_configured():
            self._warn_once("yt-config", f"YouTube skipped: {config.YOUTUBE_CLIENT_SECRET_FILE} not found")
            return
        if self.youtube_blocked:
            return
        if youtube_upload.uploads_left_today() <= 0:
            self.youtube_blocked = "daily cap reached"
            self._warn_once("yt-cap", f"YouTube daily cap ({config.YOUTUBE_DAILY_LIMIT}) reached — "
                                      "remaining clips stay pending and upload on a later run")
            return

        entry = clip["youtube"]
        try:
            result = youtube_upload.upload_short(
                os.path.join(video_dir, clip["file"]), clip["title"], clip["caption"], clip["hashtags"]
            )
            entry.update(status="done", **result)
            print(f"   YouTube  ✓ {clip['file']} -> {result['url']}")
        except youtube_upload.QuotaExceeded as e:
            self.youtube_blocked = str(e)  # not the clip's fault: stays pending, attempts not counted
            self._warn_once("yt-cap", f"YouTube cap reached ({e}) — remaining clips stay pending")
        except youtube_upload.LoginNeeded as e:
            self.youtube_blocked = str(e)  # same: clips wait until the login is fixed
            self._warn_once("yt-login", f"YouTube skipped: {e}")
        except Exception as e:  # noqa: BLE001 — one bad upload must not stop the others
            entry["status"] = "error"
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["error"] = str(e)[:500]
            print(f"   YouTube  ✗ {clip['file']}: {e}")

    def upload_instagram(self, clip, video_dir):
        from splitter import instagram_upload

        if not config.instagram_configured():
            self._warn_once("ig-config", "Instagram skipped: add INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID to .env")
            return

        entry = clip["instagram"]
        caption = f"{clip['caption']}\n\n{' '.join(clip['hashtags'])}".strip()
        try:
            result = instagram_upload.upload_reel(os.path.join(video_dir, clip["file"]), caption)
            entry.update(status="done", **result)
            print(f"   Instagram ✓ {clip['file']} -> {result['url'] or result['id']}")
        except Exception as e:  # noqa: BLE001
            entry["status"] = "error"
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["error"] = str(e)[:500]
            print(f"   Instagram ✗ {clip['file']}: {e}")

    def upload_video_dir(self, video_dir):
        state = load_state(video_dir)
        if not state:
            return
        for clip in state["clips"]:
            if not os.path.exists(os.path.join(video_dir, clip["file"])):
                continue  # already deleted after a finished upload
            if needs_upload(clip, "youtube"):
                self.upload_youtube(clip, video_dir)
                save_state(video_dir, state)  # save after every upload so a crash loses nothing
            if needs_upload(clip, "instagram"):
                self.upload_instagram(clip, video_dir)
                save_state(video_dir, state)
            delete_if_finished(clip, video_dir)


def delete_if_finished(clip, video_dir):
    """Free disk space: remove the .mp4 once every configured platform is done (or gave up)."""
    if not config.DELETE_AFTER_UPLOAD:
        return
    if any(clip[p]["status"] == "review" for p in ("youtube", "instagram")):
        return  # still waiting for you to look at it
    platforms = [p for p, ok in (("youtube", config.youtube_configured()),
                                 ("instagram", config.instagram_configured())) if ok]
    if platforms and not any(needs_upload(clip, p) for p in platforms):
        path = os.path.join(video_dir, clip["file"])
        if os.path.exists(path):
            os.remove(path)


def upload_pending(uploader, skip_dir=None):
    """Upload clips from earlier runs that are still waiting (oldest video first)."""
    dirs = sorted(glob.glob(os.path.join(config.OUTPUT_DIR, "*", "uploads.json")), key=os.path.getmtime)
    waiting = []
    for path in dirs:
        video_dir = os.path.dirname(path)
        state = load_state(video_dir)
        if video_dir != skip_dir and any(
            needs_upload(c, p) and os.path.exists(os.path.join(video_dir, c["file"]))
            for c in state["clips"] for p in ("youtube", "instagram")
        ):
            waiting.append(video_dir)

    if waiting:
        print(f"\n== Uploading pending clips from {len(waiting)} earlier video(s) ==")
        for video_dir in waiting:
            uploader.upload_video_dir(video_dir)


# ---------- the pipeline for one link ----------

def process(url, uploader, review=False):
    """Link -> clips. With review=True (web page) it stops when the clips are made and waits
    for you to press "Post"; otherwise (command line) it uploads straight away."""
    print("\n[1/5] Getting video info...")
    info = get_video_info(url)
    video_dir = os.path.join(config.OUTPUT_DIR, info["id"])
    os.makedirs(video_dir, exist_ok=True)
    print(f"   {info['title']}  ({fmt(info['duration'])})")

    state = load_state(video_dir)
    if state and review and not any(
        os.path.exists(os.path.join(video_dir, c["file"])) for c in state["clips"]
    ) and not any(c[p]["status"] == "done" for c in state["clips"] for p in ("youtube", "instagram")):
        # Same link again, but its clips were cleaned up and nothing was posted: make them again
        # (transcript + AI moments are reused, only the cutting is redone).
        os.remove(os.path.join(video_dir, "uploads.json"))
        state = None

    if not state:
        print("[2/5] Downloading transcript...")
        transcript = get_transcript(info["url"], video_dir)
        if not transcript:
            print("   This video has no captions (manual or auto) — can't find highlights. Stopping.")
            return
        print(f"   {os.path.basename(transcript)}")

        print("[3/5] Finding hyped moments with AI...")
        highlights_file = os.path.join(video_dir, "highlights.json")
        if os.path.exists(highlights_file):
            with open(highlights_file, encoding="utf-8") as f:
                moments = json.load(f)
        else:
            moments = find_highlights(transcript, info["title"], info["duration"])
            with open(highlights_file, "w", encoding="utf-8") as f:
                json.dump(moments, f, ensure_ascii=False, indent=2)
        if not moments:
            print("   The AI found no usable moments. Stopping.")
            return
        for m in moments:
            print(f"   {fmt(m['start'])}-{fmt(m['end'])}  [{m['score']}/10]  {m['title']}")

        print("[4/5] Downloading only those parts and making vertical clips...")
        paths = make_clips(info["url"], moments, video_dir, info["duration"])

        first = "review" if review else "pending"
        state = {
            "video": {**info, "source": "youtube"},
            "created": time.time(),
            "clips": [
                {
                    "file": os.path.basename(p),
                    **{k: m[k] for k in ("start", "end", "title", "caption", "hashtags")},
                    "youtube": {"status": first},
                    "instagram": {"status": first},
                }
                for p, m in zip(paths, moments)
            ],
        }
        save_state(video_dir, state)
        cleanup_old_videos()
    elif review:
        state["created"] = time.time()  # used again: counts as the newest video
        save_state(video_dir, state)
        print("   Already processed before.")
    else:
        print("   Already processed before — only uploading what's still pending.")

    if review:
        print("[5/5] Clips ready — review them and press Post.")
        return video_dir

    print("[5/5] Uploading...")
    uploader.upload_video_dir(video_dir)
    return video_dir


# ---------- your own video (web page drag & drop) ----------

def process_file(src_path, original_name, title, caption, hashtags, platforms, vertical, uploader):
    """A video you dropped on the page: no transcript/AI/cutting, just (if needed) convert, then upload."""
    from splitter.clips import prepare_upload

    video_id = f"upload-{time.strftime('%Y%m%d-%H%M%S')}"
    video_dir = os.path.join(config.OUTPUT_DIR, video_id)
    os.makedirs(video_dir, exist_ok=True)

    print(f"\n[1/2] Preparing {original_name}...")
    clip = os.path.join(video_dir, "clip_01.mp4")
    try:
        prepare_upload(src_path, clip, vertical)
    except BaseException:
        import shutil
        shutil.rmtree(video_dir, ignore_errors=True)
        raise
    duration = media_duration(clip)

    title = title.strip() or os.path.splitext(original_name)[0][:100] or "My video"
    state = {
        "video": {"id": video_id, "title": title, "duration": duration, "url": None,
                  "source": "upload", "filename": original_name},
        "created": time.time(),
        "clips": [{
            "file": "clip_01.mp4", "start": 0, "end": duration,
            "title": title, "caption": caption.strip(), "hashtags": clean_hashtags(hashtags),
            **{p: {"status": "pending" if p in platforms else "skipped"} for p in ("youtube", "instagram")},
        }],
    }
    save_state(video_dir, state)
    cleanup_old_videos()

    print("[2/2] Uploading...")
    uploader.upload_video_dir(video_dir)
    return video_dir


def media_duration(path):
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return round(float(result.stdout.strip()), 1)
    except ValueError:
        return 0


def clean_hashtags(value):
    words = value.split() if isinstance(value, str) else list(value or [])
    tags = []
    for w in words:
        w = w.strip().strip(",")
        if w and w != "#":
            tags.append(w if w.startswith("#") else f"#{w}")
    return tags[:15]


# ---------- review: post / discard ----------

def approve(video_dir, picks, platforms):
    """You pressed Post. picks: [{file, title, caption, hashtags}] = the clips to post (with your edits).
    Clips not in picks are skipped and deleted. Returns how many clips will be uploaded."""
    with state_lock:
        state = load_state(video_dir)
        if not state or not in_review(state):
            raise ValueError("these clips were already posted or discarded")
        edits = {p["file"]: p for p in picks}
        count = 0
        for clip in state["clips"]:
            pick = edits.get(clip["file"])
            if pick and os.path.exists(os.path.join(video_dir, clip["file"])):
                if pick.get("title", "").strip():
                    clip["title"] = pick["title"].strip()[:100]
                if "caption" in pick:
                    clip["caption"] = pick["caption"].strip()[:2000]
                if "hashtags" in pick:
                    clip["hashtags"] = clean_hashtags(pick["hashtags"])
                for p in ("youtube", "instagram"):
                    clip[p] = {"status": "pending" if p in platforms else "skipped"}
                count += 1
            else:
                for p in ("youtube", "instagram"):
                    clip[p] = {"status": "skipped"}
                _remove_file(os.path.join(video_dir, clip["file"]))
        save_state(video_dir, state)
        return count


def discard(video_dir):
    with state_lock:
        state = load_state(video_dir)
        if not state or not in_review(state):
            raise ValueError("these clips were already posted or discarded")
        for clip in state["clips"]:
            for p in ("youtube", "instagram"):
                if clip[p]["status"] == "review":
                    clip[p] = {"status": "skipped"}
            _remove_file(os.path.join(video_dir, clip["file"]))
        save_state(video_dir, state)


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------- disk space: keep only the newest few videos ----------

def video_dirs():
    """All processed videos, newest first: [(video_dir, state)]."""
    found = []
    for path in glob.glob(os.path.join(config.OUTPUT_DIR, "*", "uploads.json")):
        video_dir = os.path.dirname(path)
        try:
            state = load_state(video_dir)
        except (OSError, ValueError):
            continue
        if state:
            state.setdefault("created", os.path.getctime(path))
            found.append((video_dir, state))
    found.sort(key=lambda d: d[1]["created"], reverse=True)
    return found


def cleanup_old_videos(keep=None):
    """Only the newest `keep` videos (KEEP_VIDEOS, default 3) keep their files on disk.
    Older ones lose their .mp4 clips and transcripts; uploads.json stays so their links stay listed."""
    with state_lock:
        keep = config.KEEP_VIDEOS if keep is None else keep
        with_files = [
            (d, s) for d, s in video_dirs()
            if glob.glob(os.path.join(glob.escape(d), "*.mp4"))
        ]
        for video_dir, state in with_files[keep:]:
            for pattern in ("*.mp4", "*.part", "*.json3", "*.vtt", "*.m4a", "*.webm"):
                for f in glob.glob(os.path.join(glob.escape(video_dir), pattern)):
                    _remove_file(f)
            changed = False
            for clip in state["clips"]:
                for p in ("youtube", "instagram"):
                    if clip[p]["status"] in ("review", "pending", "error"):
                        clip[p] = {**clip[p], "status": "skipped", "error": "deleted to free space (only the newest "
                                                                           f"{keep} videos are kept)"}
                        changed = True
            if changed:
                save_state(video_dir, state)
            print(f"   cleaned up old video: {state['video'].get('title') or os.path.basename(video_dir)}")


def summary():
    pending = {"youtube": 0, "instagram": 0}
    for path in glob.glob(os.path.join(config.OUTPUT_DIR, "*", "uploads.json")):
        for clip in load_state(os.path.dirname(path))["clips"]:
            for platform in pending:
                if needs_upload(clip, platform):
                    pending[platform] += 1
    if any(pending.values()):
        print(f"\nStill waiting to upload: {pending['youtube']} to YouTube, {pending['instagram']} to Instagram."
              "\nRun `uv run splitter --pending` later (YouTube's cap resets at midnight Pacific time).")
    else:
        print("\nAll clips uploaded.")


def main():
    # Windows consoles default to cp1252: printing ✓ or a video title with emoji would crash
    # right after a successful upload (and the clip would be posted twice). Never let that happen.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:]
    only_pending = "--pending" in args

    if "--serve" in args:
        from splitter.server import serve
        serve()
        return

    if "--login" in args:
        from splitter.youtube_upload import login
        login()
        return
    links = [a for a in args if not a.startswith("--")]

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    uploader = Uploader()

    if only_pending:
        upload_pending(uploader)
        summary()
        return

    url = links[0] if links else input("Enter YouTube URL: ").strip()
    if not url:
        raise SystemExit("No link given.")

    upload_pending(uploader)
    try:
        process(url, uploader)
    except KeyboardInterrupt:
        raise SystemExit("\nStopped. Run the same link again to continue where it left off.")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit("\nSomething failed. Fix the error above and run the same link again — finished steps are skipped.")
    summary()


if __name__ == "__main__":
    main()
