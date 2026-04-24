from __future__ import annotations

"""Amazon Music metadata helpers (yt-dlp based)."""

import json
import logging
import subprocess

log = logging.getLogger("spotify_dl")

__all__ = [
    "get_amazon_track_info",
]


def get_amazon_track_info(track_id: str, ytdlp_bin: str) -> dict:
    """Extract Amazon Music track metadata via yt-dlp."""
    url = f"https://music.amazon.com/tracks/{track_id}"
    info: dict = {
        "title": "", "artists": [], "album": "",
        "release_date": "", "cover_url": "",
        "track_number": 1, "disc_number": 1, "isrc": None,
        "track_id": None,
        "amazon_id": track_id,
        "amazon_url": url,
    }

    try:
        result = subprocess.run(
            [ytdlp_bin, "--dump-json", "--quiet", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            info["title"]        = data.get("title") or ""
            info["album"]        = data.get("album") or ""
            info["release_date"] = data.get("release_date") or data.get("upload_date") or ""
            info["cover_url"]    = data.get("thumbnail") or ""
            info["isrc"]         = data.get("isrc") or None
            artist = data.get("artist") or data.get("uploader") or ""
            if artist:
                info["artists"] = [artist]
    except Exception as exc:
        log.warning("amazon yt-dlp json failed: %s", exc)

    return info
