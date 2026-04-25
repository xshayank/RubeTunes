from __future__ import annotations

"""Streaming-manifest parser for monochrome/Tidal streams.

Ported from monochrome-music/monochrome:
  - js/api.js — extractStreamUrlFromManifest()  (lines ~265-330)
  - js/api.js — getTrackManifestFormats()        (lines ~397-409)
  - js/api.js — getAudioQualityFromManifestFormats() (lines ~415-423)
  - js/api.js — normalizeTrackManifestResponse() (lines ~426-490)

Supported manifest types
------------------------
1. **OriginalTrackUrl** – a plain HTTPS URL returned directly by some proxy
   instances alongside the manifest.  Always preferred.
2. **BTS JSON** – a base64-encoded JSON blob ``{"urls": ["https://..."]}``
   returned by older Tidal API clients.  May contain multiple URLs ranked by
   quality keyword.
3. **DASH MPD** – a base64-encoded XML string starting with ``<MPD``.
   Only the first segment URL is extracted here (full DASH download is
   handled in download.py).
4. **HLS** – a ``.m3u8`` URL; returned as-is for the downloader to handle.
5. **Raw object** – a ``{"urls": [...]}`` dict (already decoded).
"""

import base64
import json
import logging
import re

from rubetunes.providers.monochrome.constants import (
    FORMAT_TO_QUALITY,
    QUALITY_FALLBACK_CHAIN,
    QUALITY_HIGH,
    QUALITY_LOW,
    QUALITY_TO_FORMATS,
)

log = logging.getLogger(__name__)

# Quality keywords used to sort URL lists when multiple URLs are present.
# Source: js/api.js#L276-L292 (priority sort inside extractStreamUrlFromManifest)
_PRIORITY_KEYWORDS = ["flac", "lossless", "hi-res", "high"]


def _rank_url(url: str) -> int:
    """Return a sort key (lower = better) based on quality keywords in the URL."""
    low = url.lower()
    for i, kw in enumerate(_PRIORITY_KEYWORDS):
        if kw in low:
            return i
    return len(_PRIORITY_KEYWORDS)


def _best_url(urls: list[str]) -> str | None:
    """Return the best URL from a list by quality-keyword ranking."""
    if not urls:
        return None
    return min(urls, key=_rank_url)


def extract_stream_url(manifest: str | dict | None) -> str | None:
    """Extract a playable URL from a Tidal stream manifest.

    Parameters
    ----------
    manifest:
        One of:
        - A base64-encoded string (BTS JSON *or* DASH MPD XML)
        - A raw ``{"urls": [...]}`` dict
        - A plain HTTPS URL string (passed through as-is)
        - ``None`` → returns ``None``

    Returns
    -------
    A plain HTTPS URL string, or ``None`` if extraction fails.

    Source: js/api.js — extractStreamUrlFromManifest() (lines ~265-330)
    """
    if manifest is None:
        return None

    # ── Case 1: dict with "urls" key (already-decoded object) ──────────────
    if isinstance(manifest, dict):
        urls = manifest.get("urls")
        if urls and isinstance(urls, list):
            return _best_url(urls)
        return None

    if not isinstance(manifest, str):
        return None

    # ── Case 2: plain HTTPS/HTTP URL ────────────────────────────────────────
    if manifest.startswith("http://") or manifest.startswith("https://"):
        return manifest

    # ── Case 3: base64-encoded payload ──────────────────────────────────────
    try:
        decoded = base64.b64decode(manifest).decode("utf-8", errors="replace")
    except Exception:
        log.debug("manifest is not base64; treating as plain URL")
        return manifest if manifest.startswith("http") else None

    # ── Case 3a: DASH MPD ───────────────────────────────────────────────────
    if "<MPD" in decoded:
        # Extract the first <BaseURL> element; full DASH segmented download
        # is handled by the downloader, but we return the manifest text so
        # the caller can pass it to DashDownloader.
        # Source: js/api.js#L302-L305 (blob URL creation for DASH)
        match = re.search(r"<BaseURL[^>]*>([^<]+)</BaseURL>", decoded)
        if match:
            return match.group(1).strip()
        # Return None here; callers must detect "<MPD" in decoded and use
        # the dash-downloader path instead.
        log.debug("DASH manifest detected but no BaseURL found")
        return None

    # ── Case 3b: BTS JSON ───────────────────────────────────────────────────
    try:
        parsed = json.loads(decoded)
        urls = parsed.get("urls")
        if urls and isinstance(urls, list):
            return _best_url(urls)
        # Single URL field
        url = parsed.get("url") or parsed.get("streamUrl")
        if url:
            return url
    except json.JSONDecodeError:
        pass

    # ── Case 3c: bare URL embedded in the decoded string ───────────────────
    match = re.search(r"https?://[\w\-.~:?#\[\]@!$&'()*+,;=%/]+", decoded)
    if match:
        return match.group(0)

    return None


def is_dash_manifest(manifest: str | None) -> bool:
    """Return True if the manifest encodes a DASH MPD XML document.

    Source: js/api.js#L302-L305
    """
    if not manifest or not isinstance(manifest, str):
        return False
    try:
        decoded = base64.b64decode(manifest).decode("utf-8", errors="replace")
        return "<MPD" in decoded
    except Exception:
        return False


def get_decoded_dash_xml(manifest: str) -> str | None:
    """Decode a base64 DASH manifest to raw XML text, or None on failure."""
    try:
        return base64.b64decode(manifest).decode("utf-8", errors="replace")
    except Exception:
        return None


def quality_to_formats(quality: str) -> list[str]:
    """Map a quality token to Tidal manifest format list.

    Source: js/api.js#L397-L409 (getTrackManifestFormats)
    """
    return QUALITY_TO_FORMATS.get(quality, ["FLAC"])


def formats_to_quality(formats: list[str]) -> str | None:
    """Map a list of manifest format tokens back to a quality label.

    Source: js/api.js#L415-L423 (getAudioQualityFromManifestFormats)
    """
    priority_order = [
        "EAC3_JOC",   # DOLBY_ATMOS
        "FLAC_HIRES", # HI_RES_LOSSLESS
        "FLAC",       # LOSSLESS
        "AACLC",      # HIGH
        "HEAACV1",    # LOW
    ]
    for fmt in priority_order:
        if fmt in formats:
            return FORMAT_TO_QUALITY.get(fmt)
    return None


def select_quality(requested: str, available: list[str] | None = None) -> str:
    """Return the best quality to request, with fallback chain.

    If ``available`` is provided (list of TIDAL quality tokens the track
    supports), the first entry from the fallback chain that appears in
    ``available`` is returned.  If ``available`` is None, ``requested`` is
    returned as-is (the server will downgrade if necessary).

    Source: js/api.js — enrichTrack() fallback + downloadTrack() DASH fallback
            (lines ~603-615)
    """
    if available is None:
        return requested

    # Walk fallback chain starting from requested quality
    try:
        start = QUALITY_FALLBACK_CHAIN.index(requested)
    except ValueError:
        start = 0

    for q in QUALITY_FALLBACK_CHAIN[start:]:
        if q in available:
            return q

    # Last resort
    return QUALITY_LOW


def parse_playback_info(data: dict) -> dict:
    """Normalise a raw playbackinfo/trackManifests response dict.

    Handles both the old ``{manifest, audioQuality, ...}`` shape and the
    newer OpenAPI envelope ``{data: {attributes: {uri, formats, ...}}}``.

    Source: js/api.js — normalizeTrackManifestResponse() (lines ~426-490)
    """
    # OpenAPI envelope: data.data.attributes.uri
    raw = (
        data.get("data", {}).get("data", {}) or
        data.get("data") or
        data
    )
    attributes = raw.get("attributes", {})

    if attributes.get("uri"):
        # OpenAPI response — manifest is fetched separately (done in client.py)
        return {
            "trackId": raw.get("id"),
            "audioQuality": formats_to_quality(attributes.get("formats", [])) or QUALITY_HIGH,
            "manifestMimeType": "",
            "manifest": "",
            "formats": attributes.get("formats", []),
            "_manifest_url": attributes.get("uri"),
        }

    # Legacy/proxy response — manifest is already embedded
    return {
        "trackId": raw.get("trackId") or raw.get("id"),
        "audioQuality": raw.get("audioQuality", QUALITY_HIGH),
        "manifestMimeType": raw.get("manifestMimeType", ""),
        "manifest": raw.get("manifest", ""),
        "OriginalTrackUrl": raw.get("OriginalTrackUrl") or raw.get("originalTrackUrl"),
        "bitDepth": raw.get("bitDepth"),
        "sampleRate": raw.get("sampleRate"),
        "trackReplayGain": raw.get("trackReplayGain") or raw.get("replayGain"),
        "trackPeakAmplitude": raw.get("trackPeakAmplitude") or raw.get("peakAmplitude"),
        "albumReplayGain": raw.get("albumReplayGain"),
        "albumPeakAmplitude": raw.get("albumPeakAmplitude"),
        "formats": raw.get("formats", []),
    }
