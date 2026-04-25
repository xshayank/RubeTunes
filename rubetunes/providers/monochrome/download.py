from __future__ import annotations

"""Track download helper for the monochrome/Tidal provider.

Ported from monochrome-music/monochrome:
  - js/api.js — downloadTrack()  (lines ~615-720)
  - js/api.js — getStreamUrl()   (lines ~557-590)
  - js/api.js — extractStreamUrlFromManifest() (lines ~265-330)

This module handles:
1. Resolving a track ID + quality to a direct download URL.
2. Streaming the file in chunks (range-request compatible).
3. Writing to disk with the correct file extension.
4. Embedding cover art and ID3/Vorbis/FLAC tags via mutagen.

Quality fallback chain (same as upstream)
-----------------------------------------
HI_RES_LOSSLESS → LOSSLESS → HIGH → LOW

Format → extension mapping
--------------------------
FLAC / FLAC_HIRES → .flac
AACLC / M4A       → .m4a
HEAACV1           → .aac
EAC3_JOC          → .flac  (transcoded Dolby Atmos — store as FLAC)
"""

import io
import logging
from pathlib import Path
from typing import Callable

import httpx

from rubetunes.providers.monochrome.constants import (
    QUALITY_LOSSLESS,
    REQUEST_TIMEOUT,
)
from rubetunes.providers.monochrome.manifest import (
    extract_stream_url,
)
from rubetunes.providers.monochrome.models import StreamInfo, Track

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension mapping
# Source: js/api.js — downloadTrack(), getExtensionFromBlob() usage
# ---------------------------------------------------------------------------
_QUALITY_EXTENSION: dict[str, str] = {
    "HI_RES_LOSSLESS": "flac",
    "LOSSLESS":        "flac",
    "HIGH":            "m4a",
    "LOW":             "aac",
    "DOLBY_ATMOS":     "flac",
}

_FORMAT_EXTENSION: dict[str, str] = {
    "FLAC_HIRES": "flac",
    "FLAC":       "flac",
    "AACLC":      "m4a",
    "HEAACV1":    "aac",
    "EAC3_JOC":   "flac",
}

_CHUNK_SIZE: int = 1024 * 256  # 256 KiB per chunk


def extension_for_quality(quality: str, formats: list[str] | None = None) -> str:
    """Return the file extension for a given quality/format combination.

    Source: js/api.js (getExtensionFromBlob + downloadTrack output filename)
    """
    if formats:
        for fmt in formats:
            if fmt in _FORMAT_EXTENSION:
                return _FORMAT_EXTENSION[fmt]
    return _QUALITY_EXTENSION.get(quality, "flac")


async def resolve_stream_url(
    stream_info: StreamInfo,
) -> str:
    """Extract a playable HTTPS URL from a StreamInfo object.

    Source: js/api.js — getStreamUrl() (lines ~557-590)
            extractStreamUrlFromManifest() (lines ~265-330)
    """
    if stream_info.original_track_url:
        return stream_info.original_track_url

    if stream_info.manifest:
        url = extract_stream_url(stream_info.manifest)
        if url:
            return url

    raise ValueError(
        f"Cannot resolve stream URL for track {stream_info.track_id}: "
        "no OriginalTrackUrl and manifest yielded no URL"
    )


async def download_track(
    track: Track,
    stream_info: StreamInfo,
    output_path: Path,
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
    embed_tags: bool = True,
    cover_url: str | None = None,
) -> Path:
    """Download a track and write it to *output_path*.

    Parameters
    ----------
    track:
        Track metadata (used for tagging).
    stream_info:
        StreamInfo returned by ``MonochromeClient.get_stream_info()``.
    output_path:
        Destination file path.  The extension is corrected automatically if
        it does not match the detected audio format.
    on_progress:
        Optional callback ``(received_bytes, total_bytes_or_None)`` called
        after each chunk.
    embed_tags:
        When True, embed ID3/Vorbis/FLAC tags and cover art (requires mutagen).
    cover_url:
        URL of the cover image to embed.  If None, no cover art is embedded.

    Returns
    -------
    Path to the written file (may differ from *output_path* if the extension
    was corrected).

    Source: js/api.js — downloadTrack() (lines ~615-720)
    """
    url = await resolve_stream_url(stream_info)

    quality = stream_info.audio_quality or QUALITY_LOSSLESS
    ext = extension_for_quality(quality, stream_info.formats)

    # Correct extension if needed
    out = output_path.with_suffix(f".{ext}")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        # HEAD request to get Content-Length without fetching the whole body
        # Source: js/api.js#L657-L666 (HEAD request before GET)
        total: int | None = None
        try:
            head = await client.head(url)
            if head.is_success:
                cl = head.headers.get("content-length")
                if cl:
                    total = int(cl)
        except Exception:
            pass

        buf = io.BytesIO()
        received = 0

        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if total is None:
                cl = resp.headers.get("content-length")
                if cl:
                    total = int(cl)

            async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                buf.write(chunk)
                received += len(chunk)
                if on_progress:
                    on_progress(received, total)

    audio_bytes = buf.getvalue()

    # Write raw bytes
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio_bytes)

    # Embed metadata + cover art
    if embed_tags:
        try:
            _embed_tags(out, track, cover_url=cover_url)
        except Exception as exc:
            log.warning("Tag embedding failed for %s: %s", out, exc)

    return out


def _embed_tags(
    path: Path,
    track: Track,
    cover_url: str | None = None,
) -> None:
    """Embed ID3/Vorbis/FLAC tags and cover art using mutagen.

    Source: js/api.js — addMetadataToAudio() call in downloadTrack()
    Tidal fields consumed: title, version, artists, album, trackNumber,
                           volumeNumber, releaseDate, copyright, isrc
    """
    try:
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import (
            APIC,
            ID3,
            TALB,
            TIT2,
            TPE1,
            TPOS,
            TRCK,
            TSRC,
        )
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        log.warning("mutagen not installed; skipping tag embedding")
        return

    ext = path.suffix.lower()

    # Cover art bytes
    cover_data: bytes | None = None
    if cover_url:
        try:
            import urllib.request
            with urllib.request.urlopen(cover_url, timeout=10) as r:
                cover_data = r.read()
        except Exception as exc:
            log.debug("Could not fetch cover art from %s: %s", cover_url, exc)

    artists = track.artist_names
    title = track.display_title
    album_title = track.album.title or ""
    track_num = str(track.track_number)
    disc_num = str(track.volume_number)

    if ext == ".flac":
        audio = FLAC(str(path))
        audio["title"] = title
        audio["artist"] = artists
        audio["album"] = album_title
        audio["tracknumber"] = track_num
        audio["discnumber"] = disc_num
        if track.isrc:
            audio["isrc"] = track.isrc
        if track.copyright:
            audio["copyright"] = track.copyright
        if cover_data:
            pic = Picture()
            pic.type = 3  # Cover (front)
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.add_picture(pic)
        audio.save()

    elif ext in (".m4a", ".mp4", ".aac"):
        audio = MP4(str(path))
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = [artists]
        audio["\xa9alb"] = [album_title]
        audio["trkn"] = [(track.track_number, 0)]
        audio["disk"] = [(track.volume_number, 0)]
        if cover_data:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()

    elif ext in (".mp3",):
        try:
            audio_id3 = ID3(str(path))
        except Exception:
            audio_id3 = ID3()
        audio_id3.add(TIT2(encoding=3, text=title))
        audio_id3.add(TPE1(encoding=3, text=artists))
        audio_id3.add(TALB(encoding=3, text=album_title))
        audio_id3.add(TRCK(encoding=3, text=track_num))
        audio_id3.add(TPOS(encoding=3, text=disc_num))
        if track.isrc:
            audio_id3.add(TSRC(encoding=3, text=track.isrc))
        if cover_data:
            audio_id3.add(
                APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data)
            )
        audio_id3.save(str(path), v2_version=3)
