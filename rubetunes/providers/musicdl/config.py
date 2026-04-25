from __future__ import annotations

"""Configuration loader for the musicdl provider.

Reads environment variables and builds the keyword-argument dicts expected
by ``musicdl.musicdl.MusicClient``.

Environment variables
---------------------
MUSICDL_DOWNLOAD_DIR
    Directory where musicdl saves downloaded files.
    Defaults to ``<repo-root>/downloads/musicdl``.
MUSICDL_DEFAULT_SOURCES
    Comma-separated list of musicdl source client names.
    Defaults to the upstream DEFAULT_MUSIC_SOURCES list.
MUSICDL_PROXY
    Optional HTTP/HTTPS proxy URL applied to all musicdl requests
    (e.g. ``http://user:pass@host:port``).
"""

import os
from pathlib import Path

__all__ = [
    "MUSICDL_DOWNLOAD_DIR",
    "MUSICDL_DEFAULT_SOURCES",
    "MUSICDL_PROXY",
    "build_init_cfg",
    "build_requests_overrides",
]

# ---------------------------------------------------------------------------
# Resolved configuration values
# ---------------------------------------------------------------------------

_BASE_DOWNLOADS = Path(os.getenv("MUSICDL_DOWNLOAD_DIR", "")).resolve() or (
    Path(__file__).resolve().parent.parent.parent.parent / "downloads" / "musicdl"
)
MUSICDL_DOWNLOAD_DIR: Path = _BASE_DOWNLOADS

_raw_sources = os.getenv("MUSICDL_DEFAULT_SOURCES", "").strip()
MUSICDL_DEFAULT_SOURCES: list[str] = (
    [s.strip() for s in _raw_sources.split(",") if s.strip()] if _raw_sources else []
)
"""Empty list means: use musicdl's own DEFAULT_MUSIC_SOURCES."""

MUSICDL_PROXY: str | None = os.getenv("MUSICDL_PROXY", "").strip() or None


# ---------------------------------------------------------------------------
# Config dict builders
# ---------------------------------------------------------------------------


def build_init_cfg(source: str) -> dict:
    """Return the ``init_music_clients_cfg`` entry for a single source."""
    MUSICDL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cfg: dict = {
        "work_dir": str(MUSICDL_DOWNLOAD_DIR / source),
        "disable_print": True,
        "auto_set_proxies": False,
        "random_update_ua": False,
    }
    return cfg


def build_requests_overrides() -> dict:
    """Return a ``requests_overrides`` dict suitable for MusicClient.

    If ``MUSICDL_PROXY`` is set, every source will route its HTTP calls
    through that proxy.
    """
    if not MUSICDL_PROXY:
        return {}
    proxy_cfg = {"proxies": {"http": MUSICDL_PROXY, "https": MUSICDL_PROXY}}
    return proxy_cfg
