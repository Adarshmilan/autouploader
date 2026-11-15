"""Settings for the whole app. Everything comes from .env (or real environment variables on the server)."""

import os

# splitter/ (the project folder, two levels up from this file)
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")


def load_env(path):
    """Read KEY=value lines from the .env file into os.environ (values already set in the terminal win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.split(" #")[0]  # allow comments after a value
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env(ENV_FILE)


def get(name, default=None):
    value = os.environ.get(name, "").strip()
    return value if value else default


# Everything the app creates or must keep between runs lives in DATA_DIR
# (clips, uploads.json, YouTube login + quota). On the cloud, mount this folder as a volume.
DATA_DIR = os.path.abspath(get("DATA_DIR", PROJECT_DIR))
OUTPUT_DIR = os.path.join(DATA_DIR, "output")


def data_path(path):
    """Relative paths in .env are inside DATA_DIR."""
    return path if os.path.isabs(path) else os.path.join(DATA_DIR, path)


# --- LLM ---
OPENROUTER_API_KEY = get("OPENROUTER_API_KEY")

# --- Transcript / clips ---
TRANSCRIPT_LANGUAGES = [lang.strip() for lang in get("TRANSCRIPT_LANGUAGES", "en").split(",")]
MAX_CLIPS = int(get("MAX_CLIPS", "3"))
CLIP_PADDING = float(get("CLIP_PADDING", "1.5"))  # seconds added before/after each moment

# --- YouTube ---
YOUTUBE_CLIENT_SECRET_FILE = data_path(get("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json"))
YOUTUBE_TOKEN_FILE = data_path("youtube_token.json")
YOUTUBE_QUOTA_FILE = data_path("youtube_quota.json")
YOUTUBE_PRIVACY = get("YOUTUBE_PRIVACY", "private")
YOUTUBE_DAILY_LIMIT = int(get("YOUTUBE_DAILY_LIMIT", "6"))

# --- Instagram ---
INSTAGRAM_ACCESS_TOKEN = get("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_USER_ID = get("INSTAGRAM_USER_ID")
# graph.instagram.com for "Instagram API with Instagram Login" tokens (start with IG...),
# graph.facebook.com for "Instagram API with Facebook Login" tokens (start with EAA...).
INSTAGRAM_API_HOST = get("INSTAGRAM_API_HOST", "graph.instagram.com")
INSTAGRAM_API_VERSION = get("INSTAGRAM_API_VERSION", "v23.0")

# --- Web server mode (splitter --serve) ---
APP_USER = get("APP_USER", "admin")
APP_PASSWORD = get("APP_PASSWORD")
APP_PORT = int(get("APP_PORT", "8080"))
PENDING_EVERY_HOURS = float(get("PENDING_EVERY_HOURS", "3"))  # how often waiting clips are retried

# Delete a clip's .mp4 once it's posted on every configured platform (saves server disk).
DELETE_AFTER_UPLOAD = get("DELETE_AFTER_UPLOAD", "true").lower() == "true"

# --- YouTube downloading from a cloud server ---
# YouTube often blocks datacenter IPs ("Sign in to confirm you're not a bot").
# Fixes: cookies exported from a logged-in browser, and/or a residential proxy.
YTDLP_COOKIES_FILE = get("YTDLP_COOKIES_FILE")
YTDLP_PROXY = get("YTDLP_PROXY")

# yt-dlp options shared by every step that talks to YouTube.
YTDLP_BASE = {
    "js_runtimes": {"node": {}},          # Node.js solves YouTube's JS challenges so all formats show up
    "remote_components": ["ejs:github"],  # lets yt-dlp fetch its official challenge-solver script
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "retries": 10,
    "fragment_retries": 10,
}
if YTDLP_COOKIES_FILE:
    YTDLP_BASE["cookiefile"] = data_path(YTDLP_COOKIES_FILE)
if YTDLP_PROXY:
    YTDLP_BASE["proxy"] = YTDLP_PROXY


def youtube_configured():
    # The server needs only the saved login (youtube_token.json, or YOUTUBE_TOKEN_JSON in .env).
    return bool(get("YOUTUBE_TOKEN_JSON")) or os.path.exists(YOUTUBE_TOKEN_FILE) or os.path.exists(YOUTUBE_CLIENT_SECRET_FILE)


def instagram_configured():
    return bool(INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID)
