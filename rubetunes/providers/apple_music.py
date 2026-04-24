from __future__ import annotations

"""Apple Music / iTunes Search API enrichment (C4).

Uses the free iTunes Search API (no auth required) to:
  - Fetch higher-resolution cover art (often 1400×1400)
  - Fill missing track-number / disc-number / release-date fields

Usage::

    from rubetunes.providers.apple_music import enrich_from_apple_music

    info = enrich_from_apple_music(info)
"""

import logging
import re
import urllib.parse

import requests

log = logging.getLogger(__name__)

_ITUNES_SEARCH = "https://itunes.apple.com/search"
_TIMEOUT = 8

__all__ = ["enrich_from_apple_music", "fetch_apple_cover"]


def _search_itunes(query: str, media: str = "music", limit: int = 1) -> list[dict]:
    """Search the iTunes API and return the raw result items."""
    try:
        resp = requests.get(
            _ITUNES_SEARCH,
            params={"term": query, "media": media, "entity": "song", "limit": limit},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as exc:
        log.debug("iTunes search failed for %r: %s", query, exc)
        return []


def _upscale_artwork_url(url: str, size: int = 1400) -> str:
    """Replace the artwork dimensions in an iTunes artwork URL."""
    # e.g. https://is5.mzstatic.com/image/thumb/.../100x100bb.jpg → .../1400x1400bb.jpg
    return re.sub(r"\d+x\d+bb", f"{size}x{size}bb", url)


def fetch_apple_cover(title: str, artist: str, album: str = "") -> str | None:
    """Return a high-resolution Apple Music cover URL or None."""
    query = f"{title} {artist} {album}".strip()
    results = _search_itunes(query)
    if not results:
        return None
    raw_url = results[0].get("artworkUrl100") or results[0].get("artworkUrl60")
    if not raw_url:
        return None
    return _upscale_artwork_url(raw_url, 1400)


def enrich_from_apple_music(info: dict) -> dict:
    """Add missing metadata fields from iTunes Search API.

    Fields populated if missing: cover_url (high-res), track_number,
    disc_number, release_date.
    Returns the (potentially mutated) *info* dict.
    """
    title = info.get("title", "")
    artist = ", ".join(info.get("artists", [])) if info.get("artists") else ""
    album = info.get("album", "")

    if not title or not artist:
        return info

    query = f"{title} {artist} {album}".strip()
    results = _search_itunes(query)
    if not results:
        return info

    result = results[0]

    # High-res cover art (B10)
    raw_cover = result.get("artworkUrl100") or result.get("artworkUrl60")
    if raw_cover:
        big_cover = _upscale_artwork_url(raw_cover, 1400)
        # Only replace if the current cover is smaller or absent
        current = info.get("cover_url", "")
        if not current or "640" not in current:
            info["cover_url"] = big_cover
            log.debug("Apple Music cover: %s", big_cover)

    # Track number
    if not info.get("track_number") and result.get("trackNumber"):
        info["track_number"] = result["trackNumber"]

    # Disc number
    if not info.get("disc_number") and result.get("discNumber"):
        info["disc_number"] = result["discNumber"]

    # Release date (ISO 8601 like "2021-06-25T07:00:00Z")
    if not info.get("release_date") and result.get("releaseDate"):
        # Keep only the date part
        info["release_date"] = result["releaseDate"][:10]

    return info
