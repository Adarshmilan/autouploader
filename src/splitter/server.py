"""Always-on mode for a server: a small password-protected web page to submit links.

    uv run splitter --serve        (Docker: the default command in compose.yaml)

- Open http://<server-ip>:8080, log in with APP_USER / APP_PASSWORD from .env, paste a link.
- Links are processed one at a time, in order, by a background worker.
- Every PENDING_EVERY_HOURS the worker also uploads clips that were waiting
  (e.g. because YouTube's 6-per-day cap was reached) — no cron needed.
- JSON API: POST /api/run {"url": "..."}  and  GET /api/jobs   (same login, HTTP Basic auth).
"""

import base64
import hmac
import html
import io
import json
import queue
import signal
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from splitter import config
from splitter.main import Uploader, process, upload_pending

MAX_JOBS_SHOWN = 30

jobs = []                 # newest last; each: {id, url, status, added, started, finished, log}
jobs_lock = threading.Lock()
work = queue.Queue()      # job ids waiting to run


# ---------- background worker ----------

class _Tee(io.TextIOBase):
    """Send prints both to the server's log (docker logs) and to the job's log on the web page."""

    def __init__(self, job):
        self.job = job

    def write(self, text):
        sys.__stdout__.write(text)
        with jobs_lock:
            self.job["log"] = (self.job["log"] + text)[-20000:]
        return len(text)

    def flush(self):
        sys.__stdout__.flush()


def _run_job(job):
    job.update(status="running", started=_now())
    try:
        with redirect_stdout(_Tee(job)):
            print(f"== {job['url']}")
            uploader = Uploader()
            upload_pending(uploader)
            process(job["url"], uploader)
        job["status"] = "done"
    except BaseException as e:  # noqa: BLE001 — SystemExit from a step must not kill the worker
        job["status"] = "failed"
        job["log"] += f"\n{type(e).__name__}: {e}\n" + ("" if isinstance(e, SystemExit) else traceback.format_exc())
    job["finished"] = _now()


def _run_pending():
    print(f"[{_now()}] scheduled upload of waiting clips")
    try:
        upload_pending(Uploader())
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def worker():
    every = config.PENDING_EVERY_HOURS * 3600
    next_pending = time.time() + 60  # first check a minute after start
    while True:
        try:
            job_id = work.get(timeout=30)
        except queue.Empty:
            if every and time.time() >= next_pending:
                _run_pending()
                next_pending = time.time() + every
            continue
        with jobs_lock:
            job = next(j for j in jobs if j["id"] == job_id)
        _run_job(job)


def add_job(url):
    with jobs_lock:
        job = {"id": len(jobs) + 1, "url": url, "status": "queued", "added": _now(),
               "started": None, "finished": None, "log": ""}
        jobs.append(job)
    work.put(job["id"])
    return job


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- web page ----------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Splitter</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;background:#fafafa;color:#222}}
form{{display:flex;gap:8px}} input{{flex:1;padding:10px;font-size:16px}} button{{padding:10px 18px;font-size:16px}}
.job{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}}
.s{{font-weight:600}} .done{{color:#1a7f37}} .failed{{color:#c62828}} .running{{color:#b26a00}} .queued{{color:#555}}
pre{{white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:300px;overflow:auto;background:#f4f4f4;padding:8px}}
</style>{refresh}</head><body>
<h2>Splitter</h2>
<form method="post" action="/run"><input name="url" placeholder="YouTube link" required autofocus>
<button>Run</button></form>
{jobs}
</body></html>"""


def render_page():
    with jobs_lock:
        shown = list(reversed(jobs[-MAX_JOBS_SHOWN:]))
    busy = any(j["status"] in ("queued", "running") for j in shown)
    blocks = []
    for j in shown:
        blocks.append(
            f'<div class="job"><div><span class="s {j["status"]}">{j["status"].upper()}</span> '
            f'#{j["id"]} &nbsp; {html.escape(j["url"])}</div>'
            f'<small>added {j["added"]}{" · finished " + j["finished"] if j["finished"] else ""}</small>'
            f'<details{" open" if j["status"] != "done" else ""}><summary>log</summary>'
            f'<pre>{html.escape(j["log"]) or "waiting..."}</pre></details></div>'
        )
    return PAGE.format(
        refresh='<meta http-equiv="refresh" content="10">' if busy else "",
        jobs="".join(blocks) or "<p>No jobs yet.</p>",
    )


class Handler(BaseHTTPRequestHandler):
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
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _check(self):
        if self._authorized():
            return True
        self._send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="splitter"'})
        return False

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, "ok", "text/plain")  # no login: for uptime checks
        if not self._check():
            return
        if self.path == "/api/jobs":
            with jobs_lock:
                data = [{k: v for k, v in j.items() if k != "log"} for j in jobs[-MAX_JOBS_SHOWN:]]
            return self._send(200, json.dumps(data), "application/json")
        self._send(200, render_page())

    def do_POST(self):
        if not self._check():
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(min(length, 10_000)).decode()
        if self.path == "/api/run":
            url = (json.loads(raw or "{}").get("url") or "").strip()
        else:
            url = (parse_qs(raw).get("url", [""])[0]).strip()

        if not url.startswith(("http://", "https://")):
            return self._send(400, "Give a YouTube link (https://...)", "text/plain")
        job = add_job(url)

        if self.path == "/api/run":
            return self._send(202, json.dumps({"id": job["id"], "status": job["status"]}), "application/json")
        self._send(303, "", extra={"Location": "/"})

    def log_message(self, fmt, *args):
        pass  # keep docker logs for the pipeline output


def serve():
    if not config.APP_PASSWORD:
        raise SystemExit(f"Set APP_PASSWORD in {config.ENV_FILE} before running the web server.")
    # Docker stops containers with SIGTERM; exit right away (a half-done job resumes on its next run).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", config.APP_PORT), Handler)
    print(f"Splitter running on port {config.APP_PORT} — log in as '{config.APP_USER}'")
    print(f"Waiting clips are uploaded every {config.PENDING_EVERY_HOURS} h.")
    server.serve_forever()
