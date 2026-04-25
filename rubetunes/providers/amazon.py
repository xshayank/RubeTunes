from __future__ import annotations

"""Amazon Music metadata and download helpers."""

import json
import logging
import os
import subprocess
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "get_amazon_track_info",
    "_AMAZON_PROXY_BASES",
    "_AMAZON_PROXY_TIMEOUT",
    "_get_amazon_stream_url",
    "_convert_or_rename_amazon",
]

# ---------------------------------------------------------------------------
# Proxy chain for Amazon stream URLs (R3)
# Mirrors SpotiFLAC backend/amazon.go
# ---------------------------------------------------------------------------
_AMAZON_PROXY_BASES: list[str] = [
    "https://amazon.spotbye.qzz.io/api/track/{asin}",
    "https://afkar.xyz/api/amazon/track/{asin}",
]
_AMAZON_PROXY_TIMEOUT = 15


def _get_amazon_stream_url(asin: str) -> tuple[str | None, str | None]:
    """Try each proxy base in order and return (streamUrl, decryptionKey) or (None, None)."""
    for tmpl in _AMAZON_PROXY_BASES:
        url = tmpl.format(asin=asin)
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=_AMAZON_PROXY_TIMEOUT,
            )
            if not resp.ok:
                log.debug("amazon proxy %s → HTTP %d", url, resp.status_code)
                continue
            data = resp.json()
            stream_url     = data.get("streamUrl") or data.get("stream_url") or ""
            decryption_key = data.get("decryptionKey") or data.get("decryption_key") or ""
            if stream_url:
                return stream_url, decryption_key or None
        except Exception as exc:
            log.debug("amazon proxy %s: %s", url, exc)
    return None, None


# ---------------------------------------------------------------------------
# Decryption / format conversion (R4)
# ---------------------------------------------------------------------------

def _convert_or_rename_amazon(
    raw_path: Path,
    decryption_key: str,
    output_dir: Path,
    info: dict,
) -> Path:
    """Apply decryption, probe codec, convert/rename to a proper audio file.

    Steps:
    1. If *decryption_key* is non-empty: run ``ffmpeg -decryption_key`` to decrypt.
    2. Probe codec with ffprobe.
    3. If FLAC → rename to .flac.  Otherwise convert with ffmpeg → .flac.
    4. Cleanup intermediates.
    """
    from rubetunes.tagging import _safe_filename
    title  = info.get("title") or "track"
    artist = (info.get("artists") or [""])[0]
    base   = _safe_filename(f"{artist} - {title}" if artist else title)

    work_path = raw_path  # may be replaced after decryption

    # 1. Decrypt if key present
    if decryption_key:
        decrypted = output_dir / f"{base}_decrypted.tmp"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-decryption_key", decryption_key,
                    "-i", str(raw_path),
                    "-c", "copy",
                    str(decrypted),
                ],
                check=True, capture_output=True, timeout=120,
            )
            raw_path.unlink(missing_ok=True)
            work_path = decrypted
        except Exception as exc:
            log.warning("amazon decrypt failed: %s", exc)
            # Continue with undecrypted file

    # 2. Probe codec
    codec = ""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(work_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        codec = probe.stdout.strip().lower()
    except Exception as exc:
        log.debug("amazon ffprobe: %s", exc)

    # 3. Rename or convert
    if codec == "flac":
        out_path = output_dir / f"{base}.flac"
        work_path.rename(out_path)
    else:
        out_path = output_dir / f"{base}.flac"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(work_path),
                    "-vn", "-c:a", "flac",
                    str(out_path),
                ],
                check=True, capture_output=True, timeout=300,
            )
            work_path.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("amazon ffmpeg convert failed: %s — returning raw", exc)
            # Fallback: just rename with best-guess extension
            out_path = output_dir / f"{base}.m4a"
            if work_path != out_path:
                work_path.rename(out_path)

    return out_path


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

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
