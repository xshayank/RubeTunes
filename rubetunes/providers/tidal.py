from __future__ import annotations

"""Tidal API resolution helpers (requires TIDAL_TOKEN env var)."""

import logging
import os
import re
import sys

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "TIDAL_TOKEN",
    "_TIDAL_API_BASE",
    "_TIDAL_COUNTRY",
    "_tidal_headers",
    "_resolve_tidal_by_isrc",
    "_get_tidal_track",
    "_parse_tidal_track",
    "_upgrade_tidal_cover_url",
]

TIDAL_TOKEN   = os.getenv("TIDAL_TOKEN", "").strip()
_TIDAL_API_BASE = "https://api.tidal.com/v1"
_TIDAL_COUNTRY  = "US"


def _get_tidal_token() -> str:
    """Return TIDAL_TOKEN, respecting any monkey-patch on spotify_dl.TIDAL_TOKEN."""
    sdl = sys.modules.get("spotify_dl")
    if sdl is not None:
        tok = getattr(sdl, "TIDAL_TOKEN", None)
        if tok is not None:
            return tok
    return TIDAL_TOKEN


def _tidal_headers() -> dict:
    tok = _get_tidal_token()
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if tok:
        h["X-Tidal-Token"] = tok
    return h


def _resolve_tidal_by_isrc(isrc: str) -> dict | None:
    if not _get_tidal_token():
        return None
    try:
        resp = requests.get(
            f"{_TIDAL_API_BASE}/tracks",
            params={"isrc": isrc, "countryCode": _TIDAL_COUNTRY, "limit": 5},
            headers=_tidal_headers(),
            timeout=12,
        )
        if resp.ok:
            data = resp.json()
            items = data.get("items") or []
            if items:
                return items[0]
    except Exception as exc:
        log.warning("tidal ISRC lookup: %s", exc)
    return None


def _get_tidal_track(track_id: str) -> dict | None:
    if not _get_tidal_token():
        return None
    try:
        resp = requests.get(
            f"{_TIDAL_API_BASE}/tracks/{track_id}",
            params={"countryCode": _TIDAL_COUNTRY},
            headers=_tidal_headers(),
            timeout=12,
        )
        if resp.ok:
            data = resp.json()
            if data.get("id"):
                return data
    except Exception as exc:
        log.warning("tidal track get: %s", exc)
    return None


def _upgrade_tidal_cover_url(url: str) -> str:
    if not url:
        return url
    return re.sub(r'/(\d+)x(\d+)\.jpg$', '/1280x1280.jpg', url)


def _parse_tidal_track(data: dict) -> dict:
    album = data.get("album") or {}
    cover_id = album.get("cover", "").replace("-", "/")
    cover_url = _upgrade_tidal_cover_url(
        f"https://resources.tidal.com/images/{cover_id}/640x640.jpg" if cover_id else ""
    )
    release_date = album.get("releaseDate") or ""
    artists = [a.get("name", "") for a in (data.get("artists") or [])]
    if not artists and data.get("artist"):
        artists = [data["artist"].get("name", "")]
    return {
        "title":        data.get("title", ""),
        "artists":      artists,
        "album":        album.get("title", ""),
        "release_date": release_date,
        "cover_url":    cover_url,
        "track_number": data.get("trackNumber", 1),
        "disc_number":  data.get("volumeNumber", 1),
        "isrc":         data.get("isrc"),
    }
