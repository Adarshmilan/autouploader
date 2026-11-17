"""Step 4b: post a clip to Instagram as a Reel (Instagram Graph API, resumable upload of the local file)."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from splitter import config

STATUS_TIMEOUT = 600  # seconds to wait for Instagram to process the video
STATUS_POLL = 10


class InstagramError(Exception):
    pass


def _api(method, path, params=None, data=None, headers=None, base=None):
    base = base or f"https://{config.INSTAGRAM_API_HOST}/{config.INSTAGRAM_API_VERSION}"
    url = path if path.startswith("http") else f"{base}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise InstagramError(f"HTTP {e.code}: {e.read().decode()}") from e


def publishing_limit():
    """How many posts Instagram still allows in the rolling 24 h window: {"used", "total"}."""
    data = _api("GET", f"{config.INSTAGRAM_USER_ID}/content_publishing_limit", params={
        "fields": "config,quota_usage", "access_token": config.INSTAGRAM_ACCESS_TOKEN,
    }).get("data") or [{}]
    entry = data[0]
    return {"used": int(entry.get("quota_usage", 0)), "total": int((entry.get("config") or {}).get("quota_total", 50))}


def upload_reel(path, caption):
    """Post one clip as a Reel. Returns {"id", "url"}."""
    token, user_id = config.INSTAGRAM_ACCESS_TOKEN, config.INSTAGRAM_USER_ID

    # 1. Create an empty Reel "container" that we'll upload the file into.
    container = _api("POST", f"{user_id}/media", params={
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption[:2200],
        "share_to_feed": "true",
        "access_token": token,
    })
    container_id = container["id"]
    upload_uri = container.get("uri") or (
        f"https://rupload.facebook.com/ig-api-upload/{config.INSTAGRAM_API_VERSION}/{container_id}"
    )

    # 2. Send the video bytes.
    with open(path, "rb") as f:
        video = f.read()
    _api("POST", upload_uri, data=video, headers={
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(len(video)),
    })

    # 3. Wait until Instagram has processed it.
    waited = 0
    while True:
        status = _api("GET", container_id, params={"fields": "status_code,status", "access_token": token})
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED") or waited >= STATUS_TIMEOUT:
            raise InstagramError(f"processing failed: {status}")
        time.sleep(STATUS_POLL)
        waited += STATUS_POLL

    # 4. Publish.
    media = _api("POST", f"{user_id}/media_publish", params={"creation_id": container_id, "access_token": token})
    media_id = media["id"]

    try:
        link = _api("GET", media_id, params={"fields": "permalink", "access_token": token}).get("permalink")
    except InstagramError:
        link = None
    return {"id": media_id, "url": link}


if __name__ == "__main__":
    file = input("Clip to post: ").strip()
    print(upload_reel(file, os.path.basename(file)))
