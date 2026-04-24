from __future__ import annotations

"""Deezer resolution helpers."""

import logging
import re

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "_resolve_deezer",
    "_deezer_url_from_isrc",
    "_deezer_isrc_from_url",
    "_SPOTIFY_COVER_300",
    "_SPOTIFY_COVER_640",
    "_SPOTIFY_COVER_MAX",
    "_upgrade_spotify_cover_url",
]

# Spotify CDN size hash constants (Ref: SpotiFLAC backend/cover.go)
_SPOTIFY_COVER_300 = "ab67616d00001e02"
_SPOTIFY_COVER_640 = "ab67616d0000b273"
_SPOTIFY_COVER_MAX = "ab67616d000082c1"


def _upgrade_spotify_cover_url(url: str) -> str:
    """Upgrade a Spotify cover URL to maximum resolution."""
    if not url:
        return url
    if _SPOTIFY_COVER_300 in url:
        url = url.replace(_SPOTIFY_COVER_300, _SPOTIFY_COVER_MAX)
    elif _SPOTIFY_COVER_640 in url:
        url = url.replace(_SPOTIFY_COVER_640, _SPOTIFY_COVER_MAX)
    return url


def _deezer_url_from_isrc(isrc: str) -> str | None:
    """Resolve a Deezer track URL from an ISRC using the free Deezer public API."""
    try:
        url = f"https://api.deezer.com/track/isrc:{isrc.upper().strip()}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if resp.ok:
            data = resp.json()
            if "error" in data:
                return None
            link = data.get("link", "")
            track_id = data.get("id", 0)
            if link:
                return link
            if track_id:
                return f"https://www.deezer.com/track/{track_id}"
    except Exception as exc:
        log.debug("deezer isrc->url: %s", exc)
    return None


def _deezer_isrc_from_url(deezer_url: str) -> str | None:
    """Fetch the ISRC from a Deezer track using the public Deezer API."""
    try:
        m = re.search(r'/track/(\d+)', deezer_url)
        if not m:
            return None
        track_id = m.group(1)
        resp = requests.get(
            f"https://api.deezer.com/track/{track_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            isrc = data.get("isrc", "").strip().upper()
            if isrc:
                return isrc
    except Exception as exc:
        log.debug("deezer url->isrc: %s", exc)
    return None


def _resolve_deezer(isrc: str) -> dict | None:
    """Return Deezer track dict for the given ISRC, or None."""
    try:
        resp = requests.get(
            f"https://api.deezer.com/track/isrc:{isrc}",
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            if data.get("id") and "error" not in data:
                return data
    except Exception as exc:
        log.warning("deezer ISRC lookup: %s", exc)
    return None
