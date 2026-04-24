from __future__ import annotations

"""Tidal Alt proxy (no TIDAL_TOKEN required).

Port of SpotiFLAC backend/tidal_alt.go and backend/tidal_api_list.go.
"""

import base64
import json
import logging
import os
import sys

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "TIDAL_ALT_BASES",
    "_TIDAL_ALT_API_BASE",
    "_TIDAL_ALT_TIMEOUT",
    "_parse_tidal_alt_response",
    "_get_tidal_alt_url",
    "_get_tidal_alt_url_by_tidal_id",
    "_download_tidal_manifest",
    "_ext_from_manifest",
]

_TIDAL_ALT_BASES_ENV = os.getenv("TIDAL_ALT_BASES", "").strip()
TIDAL_ALT_BASES: list[str] = (
    [b.strip() for b in _TIDAL_ALT_BASES_ENV.split(",") if b.strip()]
    if _TIDAL_ALT_BASES_ENV
    else [
        "https://tidal.spotbye.qzz.io/get",
        "https://tidal.spotbye.qzz.io/tidal",
        "https://tidal2.spotbye.qzz.io/get",
        "https://tidal2.spotbye.qzz.io/tidal",
    ]
)

_TIDAL_ALT_API_BASE = TIDAL_ALT_BASES[0]
_TIDAL_ALT_TIMEOUT  = 8


def _get_tidal_alt_bases() -> list:
    """Return TIDAL_ALT_BASES, respecting any monkey-patch on spotify_dl.TIDAL_ALT_BASES."""
    sdl = sys.modules.get("spotify_dl")
    if sdl is not None:
        bases = getattr(sdl, "TIDAL_ALT_BASES", None)
        if bases is not None:
            return bases
    return TIDAL_ALT_BASES


def _parse_tidal_alt_response(resp: requests.Response) -> "str | dict | None":
    """Parse a Tidal Alt proxy response into a URL string or manifest dict."""
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if loc.startswith("http"):
            return loc

    if not resp.ok:
        return None

    ct = resp.headers.get("content-type", "")

    if "text/plain" in ct:
        txt = resp.text.strip()
        if txt.startswith("http"):
            return txt

    try:
        data = resp.json()
    except Exception:
        txt = resp.text.strip()
        if txt.startswith("http"):
            return txt
        return None

    # V2 manifest response
    manifest_b64 = (data.get("data") or {}).get("manifest")
    if manifest_b64:
        try:
            padding_needed = (4 - len(manifest_b64) % 4) % 4
            padded = manifest_b64 + "=" * padding_needed
            manifest_json = json.loads(base64.b64decode(padded))
            urls = manifest_json.get("urls") or []
            if urls:
                return {
                    "type":     "manifest",
                    "urls":     urls,
                    "codecs":   manifest_json.get("codecs", ""),
                    "mimeType": manifest_json.get("mimeType", ""),
                }
        except Exception as exc:
            log.debug("tidal manifest decode error: %s", exc)

    url = (data.get("link") or data.get("url") or "").strip()
    if url.startswith("http"):
        return url

    txt = resp.text.strip()
    if txt.startswith("http"):
        return txt

    return None


def _get_tidal_alt_url(spotify_track_id: str) -> "str | dict | None":
    """Fetch a Tidal download URL/manifest via the no-auth proxy (Spotify ID)."""
    for base in _get_tidal_alt_bases():
        try:
            resp = requests.get(
                f"{base}/{spotify_track_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=_TIDAL_ALT_TIMEOUT,
                allow_redirects=False,
            )
            result = _parse_tidal_alt_response(resp)
            if result is not None:
                return result
        except Exception as exc:
            log.debug("tidal alt proxy (%s): %s", base, exc)
    return None


def _get_tidal_alt_url_by_tidal_id(tidal_track_id: str) -> "str | dict | None":
    """Fetch a Tidal download URL/manifest via the no-auth proxy (Tidal ID)."""
    for base in _get_tidal_alt_bases():
        try:
            resp = requests.get(
                f"{base}/{tidal_track_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=_TIDAL_ALT_TIMEOUT,
                allow_redirects=False,
            )
            result = _parse_tidal_alt_response(resp)
            if result is not None:
                return result
        except Exception as exc:
            log.debug("tidal alt by tidal id (%s): %s", base, exc)
    return None


def _download_tidal_manifest(manifest: dict, out_path: "Path") -> None:  # type: ignore[name-defined]
    """Download a Tidal BTS V2 manifest by concatenating segment bytes."""
    urls = manifest.get("urls") or []
    if not urls:
        raise RuntimeError("Tidal manifest has no URLs")

    from pathlib import Path
    with open(out_path, "wb") as fout:
        for seg_url in urls:
            resp = requests.get(
                seg_url,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            for chunk in resp.iter_content(65536):
                if chunk:
                    fout.write(chunk)

    log.debug("tidal manifest: wrote %d segments to %s", len(urls), out_path.name)


def _ext_from_manifest(manifest: dict) -> str:
    codecs = (manifest.get("codecs") or "").lower()
    mime   = (manifest.get("mimeType") or "").lower()
    if "flac" in codecs or "flac" in mime:
        return ".flac"
    if "aac" in codecs or "mp4" in mime or "m4a" in mime:
        return ".m4a"
    if "mp3" in codecs or "mpeg" in mime:
        return ".mp3"
    if "opus" in codecs or "ogg" in mime:
        return ".ogg"
    return ".flac"
