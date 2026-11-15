import os

import yt_dlp

# Folder name relative to THIS file (download.py), not to where you run the script from.
# No leading "/" — "/downloadedVideos" would mean the root of the whole disk.
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloadedVideos")


def download():
    url = input("Enter YouTube URL: ")  # the text inside input() is only the prompt shown to you

    options = {
        "format": "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best",  # best video + AAC audio (plays everywhere), fallbacks after "/"
        "merge_output_format": "mp4",          # final merged file will be .mp4
        "paths": {"home": os.path.expanduser(SAVE_DIR)},  # folder the video is saved in
        "outtmpl": "%(title)s.%(ext)s",        # save file using the video's title
        "js_runtimes": {"node": {}},           # let yt-dlp use Node.js so YouTube shows all formats
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    print(f"Download complete! Saved in {os.path.expanduser(SAVE_DIR)}")

if __name__ == "__main__":
    download()
