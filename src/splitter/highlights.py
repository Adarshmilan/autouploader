"""Step 2: send the transcript to an LLM (OpenRouter) and get back the hyped moments + captions."""

import json
import re
import time
import urllib.error
import urllib.request

from splitter import config

# Free models (IDs ending in ":free" cost nothing but are often busy / rate-limited).
# OpenRouter tries them in order and uses the first one that answers.
# Other free options: https://openrouter.ai/models?max_price=0
MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "google/gemma-4-31b-it:free",
]
RETRIES = 3          # if every model is busy, wait and try again this many times
RETRY_WAIT = 20      # seconds between retries

MIN_CLIP = 5         # seconds — shorter moments are dropped
MAX_CLIP = 90        # seconds — longer moments are cut down to this

PROMPT = """You are finding the most hyped moments in a video, using its transcript, to post as
YouTube Shorts and Instagram Reels.
"Hyped" means the parts viewers would clip and share: high energy, big reveals, punchlines,
emotional peaks, strong hooks, crowd reactions, the most quotable lines.

Video title: {title}
Each transcript line starts with [start-end] times in seconds.

Return up to {max_clips} moments (several, not just one), best first. Each moment must be a
self-contained clip of 15-60 seconds, starting and ending at natural points in the speech.
Moments must not overlap.

For each moment also write the post text, in the same language as the video:
- "title": catchy Short title, max 80 characters, no hashtags
- "caption": 1-3 engaging sentences for Instagram/YouTube
- "hashtags": 3-8 relevant hashtags, each starting with #

Reply with ONLY a JSON array, no other text, in this exact shape:
[
  {{"start": <seconds>, "end": <seconds>, "score": <1-10 hype level>, "title": "...",
    "caption": "...", "hashtags": ["#..."], "reason": "<why it's hyped>"}}
]

Transcript:
{transcript}"""


def json3_to_lines(path):
    """Turn YouTube's json3 into compact lines like '[12.3-15.1] some words'."""
    with open(path, encoding="utf-8") as f:
        events = json.load(f).get("events", [])

    lines = []
    for event in events:
        text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
        text = " ".join(text.split())  # remove newlines and extra spaces
        if not text:
            continue  # json3 has empty "line break" events with no words
        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        lines.append(f"[{start:.1f}-{end:.1f}] {text}")
    return lines


def _vtt_time(stamp):
    parts = [float(p) for p in stamp.replace(",", ".").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def vtt_to_lines(path):
    """Same output as json3_to_lines, for .vtt files (backup when YouTube offers no json3)."""
    with open(path, encoding="utf-8") as f:
        blocks = f.read().split("\n\n")

    lines, last_text = [], None
    for block in blocks:
        rows = block.strip().splitlines()
        timing = next((i for i, r in enumerate(rows) if "-->" in r), None)
        if timing is None:
            continue
        start, end = rows[timing].split("-->")
        text = " ".join(re.sub(r"<[^>]+>", "", r) for r in rows[timing + 1 :])  # drop <c> styling tags
        text = " ".join(text.split())
        if not text or text == last_text:  # auto-captions repeat the previous line
            continue
        last_text = text
        lines.append(f"[{_vtt_time(start.strip()):.1f}-{_vtt_time(end.split()[0]):.1f}] {text}")
    return lines


def transcript_to_lines(path):
    return json3_to_lines(path) if path.endswith(".json3") else vtt_to_lines(path)


def ask_openrouter(prompt):
    if not config.OPENROUTER_API_KEY:
        raise SystemExit(f"Add OPENROUTER_API_KEY to {config.ENV_FILE}")

    body = {
        "models": MODELS,  # fallback list: if the first is busy, OpenRouter uses the next
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data = json.load(response)
            print(f"   answered by {data.get('model')}")
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            error = e.read().decode()
            if e.code == 429 and attempt < RETRIES:  # 429 = rate-limited / busy
                print(f"   all models busy, retrying in {RETRY_WAIT}s ({attempt}/{RETRIES})...")
                time.sleep(RETRY_WAIT)
                continue
            raise SystemExit(f"OpenRouter error {e.code}: {error}")


def parse_moments(reply):
    # Models sometimes wrap JSON in ```json ... ``` — cut out just the [ ... ] part.
    start, end = reply.find("["), reply.rfind("]")
    if start == -1 or end == -1:
        raise SystemExit(f"AI didn't return a JSON list. It said:\n{reply}")
    return json.loads(reply[start : end + 1])


def clean_moments(moments, video_duration, max_clips):
    """Fix up what the AI returned: valid times, sensible lengths, no overlaps, best first."""
    good = []
    for m in moments:
        try:
            start, end = float(m["start"]), float(m["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if video_duration:
            end = min(end, video_duration)
        end = min(end, start + MAX_CLIP)
        if end - start < MIN_CLIP:
            continue
        if any(start < g["end"] and end > g["start"] for g in good):
            continue  # overlaps a better moment we already kept

        hashtags = [h if h.startswith("#") else f"#{h}" for h in m.get("hashtags", []) if h]
        good.append({
            "start": round(start, 1),
            "end": round(end, 1),
            "score": m.get("score", 0),
            "title": str(m.get("title", "")).strip() or "Highlight",
            "caption": str(m.get("caption", "")).strip(),
            "hashtags": hashtags[:8],
            "reason": str(m.get("reason", "")).strip(),
        })

    good.sort(key=lambda g: g["score"] if isinstance(g["score"], (int, float)) else 0, reverse=True)
    return good[:max_clips]


def find_highlights(transcript_path, video_title="", video_duration=0, max_clips=None):
    max_clips = max_clips or config.MAX_CLIPS
    lines = transcript_to_lines(transcript_path)
    if not lines:
        return []

    prompt = PROMPT.format(title=video_title, max_clips=max_clips, transcript="\n".join(lines))
    print(f"   sending {len(lines)} transcript lines to OpenRouter...")
    moments = parse_moments(ask_openrouter(prompt))
    return clean_moments(moments, video_duration, max_clips)


def fmt(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


if __name__ == "__main__":
    path = input("Path to a transcript (.json3 / .vtt): ").strip()
    for m in find_highlights(path):
        print(f"{fmt(m['start'])} - {fmt(m['end'])}  [{m['score']}/10]  {m['title']}")
