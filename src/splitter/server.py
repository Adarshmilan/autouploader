"""Always-on mode for a server: a password-protected web page.

    uv run splitter --serve        (Docker: the default command in compose.yaml)

Open http://<server-ip>:8080 and log in with APP_USER / APP_PASSWORD from .env. Then either:
- paste a YouTube link -> transcript -> AI picks the hyped moments -> vertical clips
  -> you watch them on the page, choose YouTube / Instagram (both by default) and press Post; or
- drop your own video -> it's uploaded directly (no transcript / AI / cutting).

One background worker does the heavy work, one job at a time, in order.
Every PENDING_EVERY_HOURS it also uploads approved clips that were waiting
(e.g. because YouTube's daily cap was reached) — no cron needed.
Only the newest KEEP_VIDEOS (default 3) videos keep their files on disk.

JSON API (same login, HTTP Basic auth):
    GET  /api/state                        quota, jobs, clips waiting for review, history
    POST /api/run      {"url"}             start a YouTube link
    POST /api/post     {"video_id", "clips": [{file, title, caption, hashtags}], "platforms": [...]}
    POST /api/discard  {"video_id"}
    POST /api/upload?name=&title=&caption=&hashtags=&platforms=&vertical=   body = the video file
    GET  /media/<video_id>/<clip file>     the clip itself (for the preview player)
"""

import base64
import hmac
import io
import json
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from splitter import config
from splitter.main import (
    Uploader, approve, discard, in_review, needs_upload, process, process_file, upload_pending, video_dirs,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INCOMING_DIR = os.path.join(config.OUTPUT_DIR, "_incoming")  # dropped files land here first
PLATFORMS = ("youtube", "instagram")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp")
MAX_JOBS_SHOWN = 15
HISTORY_SHOWN = 25

jobs = []                 # newest last; each: {id, kind, label, status, added, started, finished, log, video_id}
jobs_lock = threading.Lock()
work = queue.Queue()      # (job id, function) waiting to run
next_pending = time.time() + 60  # when waiting clips are retried next


# ---------- background worker ----------

class _Tee(io.TextIOBase):
    """Send prints both to the server's log (docker logs) and to the job's log on the web page."""

    def __init__(self, job):
        self.job = job

    def write(self, text):
        try:
            sys.__stdout__.write(text)
        except (UnicodeEncodeError, OSError, AttributeError, ValueError):
            pass  # a console that can't show ✓ / emoji (Windows) must never break an upload
        with jobs_lock:
            self.job["log"] = (self.job["log"] + text)[-20000:]
        return len(text)

    def flush(self):
        sys.__stdout__.flush()


def _run_job(job, fn):
    job.update(status="running", started=_now())
    try:
        with redirect_stdout(_Tee(job)):
            video_dir = fn()
        if video_dir:
            job["video_id"] = os.path.basename(video_dir)
        job["status"] = "done" if video_dir is not False else "failed"
    except BaseException as e:  # noqa: BLE001 — SystemExit from a step must not kill the worker
        job["status"] = "failed"
        job["log"] += f"\n{type(e).__name__}: {e}\n" + ("" if isinstance(e, SystemExit) else traceback.format_exc())
    job["finished"] = _now()
    _ig_limit_cache["at"] = 0  # Instagram's remaining posts may have changed


def _run_pending():
    print(f"[{_now()}] scheduled upload of waiting clips")
    try:
        upload_pending(Uploader())
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def worker():
    global next_pending
    every = config.PENDING_EVERY_HOURS * 3600
    while True:
        try:
            job_id, fn = work.get(timeout=30)
        except queue.Empty:
            if every and time.time() >= next_pending:
                _run_pending()
                next_pending = time.time() + every
            continue
        with jobs_lock:
            job = next(j for j in jobs if j["id"] == job_id)
        _run_job(job, fn)


def add_job(kind, label, fn, video_id=None):
    with jobs_lock:
        job = {"id": len(jobs) + 1, "kind": kind, "label": label, "status": "queued", "added": _now(),
               "started": None, "finished": None, "log": "", "video_id": video_id}
        jobs.append(job)
    work.put((job["id"], fn))
    return job


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- the jobs ----------

def link_job(url):
    def run():
        print(f"== {url}")
        video_dir = process(url, Uploader(), review=True)
        return video_dir or False  # None = no captions / no moments found -> failed
    return run


def post_job(video_dir):
    def run():
        print(f"== posting clips from {os.path.basename(video_dir)}")
        uploader = Uploader()
        uploader.upload_video_dir(video_dir)
        upload_pending(uploader, skip_dir=video_dir)  # good moment to also send older waiting clips
        return video_dir
    return run


def file_job(src, name, title, caption, hashtags, platforms, vertical):
    def run():
        print(f"== your video: {name}")
        return process_file(src, name, title, caption, hashtags, platforms, vertical, Uploader())
    return run


# ---------- what the page shows ----------

_ig_limit_cache = {"at": 0, "value": None}


def instagram_limit():
    """Instagram's own 24 h posting limit (asked at most every 5 minutes)."""
    if not config.instagram_configured():
        return None
    if time.time() - _ig_limit_cache["at"] > 300:
        from splitter.instagram_upload import publishing_limit
        try:
            _ig_limit_cache["value"] = publishing_limit()
        except Exception:  # noqa: BLE001 — the page still works without this number
            _ig_limit_cache["value"] = None
        _ig_limit_cache["at"] = time.time()
    return _ig_limit_cache["value"]


def _clip_view(video_dir, video_id, clip):
    has_file = os.path.exists(os.path.join(video_dir, clip["file"]))
    return {
        "file": clip["file"],
        "src": f"/media/{video_id}/{clip['file']}" if has_file else None,
        "title": clip.get("title", ""),
        "caption": clip.get("caption", ""),
        "hashtags": clip.get("hashtags", []),
        "start": clip.get("start", 0),
        "end": clip.get("end", 0),
        **{p: {k: clip[p].get(k) for k in ("status", "url", "error", "attempts")} for p in PLATFORMS},
    }


def build_state():
    from splitter import youtube_upload

    yt_ok, ig_ok = config.youtube_configured(), config.instagram_configured()
    reviews, history = [], []
    waiting = {p: 0 for p in PLATFORMS}

    for video_dir, state in video_dirs():
        video_id = os.path.basename(video_dir)
        clips = [_clip_view(video_dir, video_id, c) for c in state["clips"]]
        for c in state["clips"]:
            for p in PLATFORMS:
                if needs_upload(c, p) and os.path.exists(os.path.join(video_dir, c["file"])):
                    waiting[p] += 1
        entry = {
            "video_id": video_id,
            "title": state["video"].get("title") or video_id,
            "url": state["video"].get("url"),
            "source": state["video"].get("source", "youtube"),
            "duration": state["video"].get("duration", 0),
            "created": state.get("created", 0),
            "clips": clips,
        }
        if in_review(state):
            entry["clips"] = [c for c in clips if c["src"]]
            if entry["clips"]:
                reviews.append(entry)
        elif len(history) < HISTORY_SHOWN:
            history.append(entry)

    with jobs_lock:
        shown = [{**j, "log": j["log"][-3000:]} for j in reversed(jobs[-MAX_JOBS_SHOWN:])]

    return {
        "youtube": {
            "configured": yt_ok,
            "left": youtube_upload.uploads_left_today(),
            "limit": config.YOUTUBE_DAILY_LIMIT,
            "reset_in": youtube_upload.seconds_until_reset(),
            "privacy": config.YOUTUBE_PRIVACY,
        },
        "instagram": {"configured": ig_ok, "limit": instagram_limit()},
        "waiting": waiting,
        "next_retry_in": max(0, int(next_pending - time.time())) if config.PENDING_EVERY_HOURS else None,
        "busy": any(j["status"] in ("queued", "running") for j in shown),
        "jobs": shown,
        "reviews": reviews,
        "history": history,
        "keep_videos": config.KEEP_VIDEOS,
        "max_upload_mb": config.MAX_UPLOAD_MB,
    }


# ---------- HTTP ----------

SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}$")
SAFE_CLIP = re.compile(r"^clip_\d{2,3}\.mp4$")


def _video_dir(video_id):
    if not isinstance(video_id, str) or not SAFE_ID.match(video_id) or video_id.startswith("_"):
        return None
    path = os.path.join(config.OUTPUT_DIR, video_id)
    return path if os.path.exists(os.path.join(path, "uploads.json")) else None


def _platforms(value):
    chosen = [p for p in PLATFORMS if p in (value or [])]
    ready = {"youtube": config.youtube_configured(), "instagram": config.instagram_configured()}
    not_ready = [p for p in chosen if not ready[p]]
    if not chosen:
        raise ValueError("pick at least one platform")
    if not_ready:
        raise ValueError(f"{' and '.join(not_ready)} isn't set up in .env")
    return chosen


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, config.APP_USER) and hmac.compare_digest(password, config.APP_PASSWORD)

    def _send(self, code, body, content_type="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _error(self, code, message):
        self._json(code, {"error": message})

    def _check(self):
        if self._authorized():
            return True
        self.close_connection = True  # an unread request body must not leak into the next request
        self._send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="splitter"'})
        return False

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("request too big")
        raw = self.rfile.read(length).decode() if length else ""
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # ----- GET -----

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except Exception as e:  # noqa: BLE001 — answer instead of dropping the connection
            traceback.print_exc()
            self.close_connection = True
            try:
                self._error(500, f"server error: {e}")
            except OSError:
                pass

    def _get(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, "ok", "text/plain")  # no login: for uptime checks
        if not self._check():
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                return self._send(200, f.read())
        if path == "/api/state":
            return self._json(200, build_state())
        if path == "/api/jobs":  # kept for scripts using the old API
            with jobs_lock:
                return self._json(200, [{k: v for k, v in j.items() if k != "log"} for j in jobs[-MAX_JOBS_SHOWN:]])
        if path.startswith("/media/"):
            return self._media(path)
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        self._error(404, "not found")

    STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

    def _static(self, name):
        """The page's own files (web/style.css, web/js/*.js). Nothing outside web/ can be read."""
        file = os.path.realpath(os.path.join(WEB_DIR, unquote(name)))
        content_type = self.STATIC_TYPES.get(os.path.splitext(file)[1].lower())
        if not content_type or not file.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(file):
            return self._error(404, "not found")
        with open(file, "rb") as f:
            self._send(200, f.read(), content_type)

    def _media(self, path):
        """Serve a clip with Range support so the preview player can seek."""
        parts = unquote(path).split("/")
        if len(parts) != 4 or not SAFE_CLIP.match(parts[3]):
            return self._error(404, "not found")
        video_dir = _video_dir(parts[2])
        file = os.path.join(video_dir, parts[3]) if video_dir else None
        if not file or not os.path.isfile(file):
            return self._error(404, "this clip was deleted")

        size = os.path.getsize(file)
        start, end = 0, size - 1
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:  # "bytes=-500" = last 500 bytes
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(file, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass  # the browser stopped this request (normal while seeking)

    # ----- POST -----

    def do_POST(self):
        if not self._check():
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/upload":
                return self._upload(parse_qs(parsed.query))
            if parsed.path == "/run":  # the old plain HTML form
                length = int(self.headers.get("Content-Length") or 0)
                url = parse_qs(self.rfile.read(min(length, 10_000)).decode()).get("url", [""])[0].strip()
                self._start_link(url)
                return self._send(303, "", extra={"Location": "/"})
            data = self._body_json()
            if parsed.path == "/api/run":
                job = self._start_link((data.get("url") or "").strip())
                return self._json(202, {"id": job["id"], "status": job["status"]})
            if parsed.path == "/api/post":
                return self._post(data)
            if parsed.path == "/api/discard":
                video_dir = _video_dir(data.get("video_id"))
                if not video_dir:
                    return self._error(404, "video not found")
                discard(video_dir)
                return self._json(200, {"ok": True})
            return self._error(404, "not found")
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 — answer instead of dropping the connection
            traceback.print_exc()
            self.close_connection = True
            return self._error(500, f"server error: {e}")

    def _start_link(self, url):
        if not re.match(r"^https?://", url):
            raise ValueError("paste a YouTube link (https://...)")
        if not re.search(r"(youtube\.com|youtu\.be)/", url, re.I):
            raise ValueError("that doesn't look like a YouTube link")
        with jobs_lock:
            for j in jobs:
                if j["kind"] == "link" and j["status"] in ("queued", "running") and j["label"] == url:
                    raise ValueError("this link is already being processed")
        return add_job("link", url, link_job(url))

    def _post(self, data):
        video_dir = _video_dir(data.get("video_id"))
        if not video_dir:
            return self._error(404, "video not found")
        platforms = _platforms(data.get("platforms"))
        picks = [c for c in data.get("clips") or [] if isinstance(c, dict) and c.get("file")]
        if not picks:
            raise ValueError("select at least one clip")
        count = approve(video_dir, picks, platforms)
        if not count:
            raise ValueError("those clips no longer exist")
        job = add_job("post", f"Posting {count} clip{'s' * (count != 1)} to {' + '.join(platforms)}",
                      post_job(video_dir), os.path.basename(video_dir))
        return self._json(202, {"id": job["id"], "count": count})

    def _upload(self, query):
        def arg(name):
            return (query.get(name) or [""])[0]

        name = os.path.basename(arg("name").replace("\\", "/")).strip() or "video.mp4"
        ext = os.path.splitext(name)[1].lower()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            if ext not in VIDEO_EXTS:
                raise ValueError(f"unsupported file type {ext or '(none)'} — use {', '.join(VIDEO_EXTS)}")
            if length <= 0:
                raise ValueError("the file is empty")
            if length > config.MAX_UPLOAD_MB * 1024 * 1024:
                raise ValueError(f"file is bigger than {config.MAX_UPLOAD_MB} MB")
            platforms = _platforms(arg("platforms").split(","))
        except ValueError:
            self.close_connection = True  # don't read the (possibly huge) body
            raise

        os.makedirs(INCOMING_DIR, exist_ok=True)
        dest = os.path.join(INCOMING_DIR, f"{uuid.uuid4().hex}{ext}")
        try:
            with open(dest, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            if os.path.exists(dest):
                os.remove(dest)
            self.close_connection = True
            return

        job = add_job("file", f"Your video: {name}",
                      file_job(dest, name, arg("title"), arg("caption"), arg("hashtags"),
                               platforms, arg("vertical") == "1"))
        self._json(202, {"id": job["id"]})

    def log_message(self, fmt, *args):
        pass  # keep docker logs for the pipeline output


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A browser closing a tab / an idle connection is normal: don't print a scary traceback.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _clean_incoming():
    """Half-received uploads from a previous run."""
    shutil.rmtree(INCOMING_DIR, ignore_errors=True)


def serve():
    if not config.APP_PASSWORD:
        raise SystemExit(f"Set APP_PASSWORD in {config.ENV_FILE} before running the web server.")
    for stream in (sys.stdout, sys.stderr):  # Windows: ✓ / emoji in titles must not crash anything
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # Docker stops containers with SIGTERM; exit right away (a half-done job resumes on its next run).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    _clean_incoming()
    threading.Thread(target=worker, daemon=True).start()
    server = _Server(("0.0.0.0", config.APP_PORT), Handler)
    server.daemon_threads = True
    print(f"Splitter running on http://localhost:{config.APP_PORT} — log in as '{config.APP_USER}'")
    print(f"Waiting clips are uploaded every {config.PENDING_EVERY_HOURS} h. "
          f"Only the newest {config.KEEP_VIDEOS} videos are kept on disk.")
    server.serve_forever()
