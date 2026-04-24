from __future__ import annotations

"""Downloader stubs — see spotify_dl.py for full implementation."""

import logging

log = logging.getLogger("spotify_dl")

__all__ = [
    "QUALITY_MP3",
    "QUALITY_FLAC_CD",
    "QUALITY_FLAC_HI",
    "QUALITY_MENU",
    "_QUALITY_LABELS",
    "download_track_from_choice",
]

QUALITY_MP3      = "mp3"
QUALITY_FLAC_CD  = "flac_cd"
QUALITY_FLAC_HI  = "flac_hi"

_QUALITY_LABELS = {
    QUALITY_MP3:     "MP3 320k",
    QUALITY_FLAC_CD: "FLAC CD (16-bit / 44.1 kHz)",
    QUALITY_FLAC_HI: "FLAC Hi-Res (24-bit)",
}

QUALITY_MENU = [
    {"label": "\U0001f3b5 MP3 320k",                    "quality": QUALITY_MP3},
    {"label": "\U0001f4bf FLAC CD (16-bit / 44.1 kHz)", "quality": QUALITY_FLAC_CD},
    {"label": "\u2b50 FLAC Hi-Res (24-bit)",            "quality": QUALITY_FLAC_HI},
]


def download_track_from_choice(
    info: dict,
    quality: str,
    output_dir: str = ".",
    ytdlp_bin: str = "yt-dlp",
    *,
    user_guid: str = "",
) -> "Path":  # type: ignore[name-defined]
    """Download a track using the specified quality tier.

    This is a thin dispatcher stub; the full implementation lives in the
    legacy spotify_dl.py until the downloader is fully ported.
    """
    raise NotImplementedError("download_track_from_choice not yet ported to rubetunes package")
