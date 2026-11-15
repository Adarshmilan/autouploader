"""Step 4a: upload a clip to YouTube as a Short, staying under the daily upload cap."""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from splitter import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class QuotaExceeded(Exception):
    """YouTube won't take more uploads today."""


# ---------- daily cap ----------
# YouTube's quota (10,000 units/day, 1,600 per upload => ~6 uploads) resets at midnight Pacific time.

def _today():
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _load_quota():
    try:
        with open(config.YOUTUBE_QUOTA_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if data.get("date") != _today():
        data = {"date": _today(), "uploads": 0}
    return data


def _save_quota(data):
    with open(config.YOUTUBE_QUOTA_FILE, "w") as f:
        json.dump(data, f)


def uploads_left_today():
    return max(0, config.YOUTUBE_DAILY_LIMIT - _load_quota()["uploads"])


def _count_upload():
    data = _load_quota()
    data["uploads"] += 1
    _save_quota(data)


def _mark_quota_used_up():
    data = _load_quota()
    data["uploads"] = max(data["uploads"], config.YOUTUBE_DAILY_LIMIT)
    _save_quota(data)


# ---------- login ----------

class LoginNeeded(Exception):
    """No valid saved YouTube login — run `splitter --login` on a PC with a browser."""


def login():
    """Run ONCE on a computer with a browser. Saves youtube_token.json for the server to use."""
    if not os.path.exists(config.YOUTUBE_CLIENT_SECRET_FILE):
        raise SystemExit(f"Put the Google OAuth client file at {config.YOUTUBE_CLIENT_SECRET_FILE}")
    flow = InstalledAppFlow.from_client_secrets_file(config.YOUTUBE_CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent", access_type="offline")
    with open(config.YOUTUBE_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Saved YouTube login to {config.YOUTUBE_TOKEN_FILE}")
    print("Copy this file into the server's data folder (or paste its contents into YOUTUBE_TOKEN_JSON in .env).")


def _load_creds():
    token_json = config.get("YOUTUBE_TOKEN_JSON")
    if os.path.exists(config.YOUTUBE_TOKEN_FILE):
        return Credentials.from_authorized_user_file(config.YOUTUBE_TOKEN_FILE, SCOPES)
    if token_json:
        return Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return None


def _get_service():
    """Uses the saved login and refreshes it silently. Never opens a browser (works on a server)."""
    creds = _load_creds()
    if not creds:
        raise LoginNeeded("no saved YouTube login — run `uv run splitter --login` on your PC")

    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Happens if the Google app is still in "Testing" mode (logins expire after 7 days)
            # or you revoked access. Fix: publish the app to "In production" and log in again.
            raise LoginNeeded(f"YouTube login expired/revoked ({e}) — run `uv run splitter --login` again") from e
        try:
            with open(config.YOUTUBE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        except OSError:
            pass  # read-only disk: fine, we refresh again next time

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---------- upload ----------

def _clean_title(title):
    title = title.replace("<", "").replace(">", "").strip()  # YouTube rejects < and >
    return (title or "Highlight")[:100]


def upload_short(path, title, description, hashtags):
    """Upload one clip. Returns {"id", "url"}. Raises QuotaExceeded when today's cap is reached."""
    if uploads_left_today() <= 0:
        raise QuotaExceeded(f"daily limit of {config.YOUTUBE_DAILY_LIMIT} uploads reached")

    tags = [h.lstrip("#") for h in hashtags]
    body = {
        "snippet": {
            "title": _clean_title(title),
            "description": f"{description}\n\n{' '.join(hashtags)} #Shorts"[:5000],
            "tags": tags,
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": config.YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }

    youtube = _get_service()
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    try:
        response = None
        while response is None:
            _, response = request.next_chunk()
    except HttpError as e:
        reasons = {err.get("reason") for err in (e.error_details or []) if isinstance(err, dict)}
        if e.resp.status == 403 and reasons & {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"}:
            _mark_quota_used_up()
            raise QuotaExceeded(f"YouTube says: {', '.join(reasons)}") from e
        raise

    _count_upload()
    video_id = response["id"]
    return {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}"}
