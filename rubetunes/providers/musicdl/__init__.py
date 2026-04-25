from __future__ import annotations

"""musicdl provider — public API.

Wraps CharlesPikachu/musicdl (PolyForm-Noncommercial-1.0.0)
https://github.com/CharlesPikachu/musicdl

Exposes:
- ``MusicdlClient``         — async wrapper around musicdl.MusicClient
- ``MusicdlTrack``          — per-track model
- ``MusicdlSearchResult``   — search result container
- ``MusicdlDownloadResult`` — download outcome container
- Error classes: ``MusicdlError``, ``MusicdlNotInstalledError``,
                 ``MusicdlSearchError``, ``MusicdlDownloadError``
"""

from rubetunes.providers.musicdl.client import MusicdlClient
from rubetunes.providers.musicdl.config import (
    MUSICDL_CONNECT_TIMEOUT,
    MUSICDL_DEFAULT_SOURCES,
    MUSICDL_DOWNLOAD_DIR,
    MUSICDL_MAX_RETRIES,
    MUSICDL_PROXY,
    MUSICDL_READ_TIMEOUT,
)
from rubetunes.providers.musicdl.errors import (
    MusicdlDownloadError,
    MusicdlError,
    MusicdlNotInstalledError,
    MusicdlSearchError,
)
from rubetunes.providers.musicdl.models import (
    MusicdlDownloadResult,
    MusicdlSearchResult,
    MusicdlTrack,
)

__all__ = [
    # Client
    "MusicdlClient",
    # Config
    "MUSICDL_DOWNLOAD_DIR",
    "MUSICDL_DEFAULT_SOURCES",
    "MUSICDL_PROXY",
    "MUSICDL_MAX_RETRIES",
    "MUSICDL_CONNECT_TIMEOUT",
    "MUSICDL_READ_TIMEOUT",
    # Models
    "MusicdlTrack",
    "MusicdlSearchResult",
    "MusicdlDownloadResult",
    # Errors
    "MusicdlError",
    "MusicdlNotInstalledError",
    "MusicdlSearchError",
    "MusicdlDownloadError",
]
