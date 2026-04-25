from __future__ import annotations

"""monochrome provider — public API.

Ported from monochrome-music/monochrome (https://github.com/monochrome-music/monochrome).

Exposes:
- ``MonochromeClient``  — async httpx client for the Tidal/monochrome API
- ``get_token``         — Tidal client-credentials token helper
- ``clear_token_cache`` — evict cached token (useful in tests)
- Model classes: ``Track``, ``Album``, ``Playlist``, ``Artist``,
                  ``SearchResult``, ``StreamInfo``, ``ArtistRef``,
                  ``TrackAlbumRef``, ``MediaMetadata``
- Manifest helpers: ``extract_stream_url``, ``is_dash_manifest``,
                    ``quality_to_formats``, ``formats_to_quality``,
                    ``select_quality``
- Download helper: ``download_track``
"""

from rubetunes.providers.monochrome.auth import clear_token_cache, get_token
from rubetunes.providers.monochrome.client import MonochromeClient
from rubetunes.providers.monochrome.download import download_track, extension_for_quality
from rubetunes.providers.monochrome.manifest import (
    extract_stream_url,
    formats_to_quality,
    is_dash_manifest,
    quality_to_formats,
    select_quality,
)
from rubetunes.providers.monochrome.models import (
    Album,
    Artist,
    ArtistRef,
    MediaMetadata,
    Playlist,
    SearchResult,
    StreamInfo,
    Track,
    TrackAlbumRef,
)

__all__ = [
    # Client
    "MonochromeClient",
    # Auth
    "get_token",
    "clear_token_cache",
    # Models
    "Track",
    "Album",
    "Playlist",
    "Artist",
    "ArtistRef",
    "TrackAlbumRef",
    "MediaMetadata",
    "SearchResult",
    "StreamInfo",
    # Manifest helpers
    "extract_stream_url",
    "is_dash_manifest",
    "quality_to_formats",
    "formats_to_quality",
    "select_quality",
    # Download
    "download_track",
    "extension_for_quality",
]
