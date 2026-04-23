# -*- coding: utf-8 -*-
"""
SpotiFLAC-style multi-platform music downloader.

Supported input sources: Spotify, Tidal, Qobuz, Amazon Music.

Resolution chain (ISRC-based):
  1. Resolve ISRC via Spotify / Tidal / Qobuz / Amazon metadata APIs
  2. Fan-out across Deezer public ISRC API, Odesli (song.link), Songstats
  3. Download via:
       – Qobuz FLAC  (proxy stream APIs, no credentials required)
       – Deezer FLAC (DEEZER_ARL cookie — if set)
       – YouTube Music MP3 320 k (always available as fallback)

Environment variables (all optional):
  SPOTIFY_TOTP_SECRET     Base-32 TOTP secret for anonymous Spotify token.
  SPOTIFY_CLIENT_ID       } Spotify app credentials; fallback if anon token fails.
  SPOTIFY_CLIENT_SECRET   }
  DEEZER_ARL              Deezer account ARL cookie — enables lossless FLAC.
  QOBUZ_APP_ID            Qobuz application ID for metadata API (optional lookup).
  TIDAL_TOKEN             Tidal client/OAuth token for metadata lookup.

  Note: Qobuz FLAC downloads use public proxy APIs and require NO credentials.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import subprocess
import time
import urllib.request
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------
SPOTIFY_TOTP_SECRET   = os.getenv("SPOTIFY_TOTP_SECRET",   "").strip()
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEEZER_ARL            = os.getenv("DEEZER_ARL",            "").strip()
QOBUZ_APP_ID          = os.getenv("QOBUZ_APP_ID",          "").strip()
# QOBUZ_EMAIL / QOBUZ_PASSWORD are no longer needed — proxy APIs used instead.
QOBUZ_EMAIL           = os.getenv("QOBUZ_EMAIL",           "").strip()
QOBUZ_PASSWORD        = os.getenv("QOBUZ_PASSWORD",        "").strip()
TIDAL_TOKEN           = os.getenv("TIDAL_TOKEN",           "").strip()

# ---------------------------------------------------------------------------
# Base-62 / GID helpers
# ---------------------------------------------------------------------------
_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b62_to_int(s: str) -> int:
    n = 0
    for c in s:
        n = n * 62 + _BASE62.index(c)
    return n


def track_id_to_gid(track_id: str) -> str:
    """Convert a 22-char base-62 Spotify track ID to a 32-char hex GID."""
    return hex(_b62_to_int(track_id))[2:].zfill(32)


def parse_spotify_track_id(text: str) -> str | None:
    """Extract the 22-char track ID from a Spotify URL, URI, or bare ID."""
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/track/([A-Za-z0-9]{22})",
        r"spotify:track:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return text
    return None


def parse_tidal_track_id(text: str) -> str | None:
    """Extract a numeric Tidal track ID from a Tidal URL."""
    text = text.strip()
    for pattern in (
        r"tidal\.com/(?:browse/)?(?:track|album/[^/]+/track)/(\d+)",
        r"listen\.tidal\.com/(?:album/[^/]+/)?track/(\d+)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def parse_qobuz_track_id(text: str) -> str | None:
    """Extract a numeric Qobuz track ID from a Qobuz URL."""
    text = text.strip()
    for pattern in (
        r"open\.qobuz\.com/track/(\d+)",
        r"qobuz\.com/[a-z\-]+/album/[^/]+/[^/]+/track/(\d+)",
        r"qobuz\.com/[a-z\-]+/track/[^/]+/(\d+)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    # bare numeric ID
    if re.fullmatch(r"\d{5,12}", text):
        return text
    return None


def parse_amazon_track_id(text: str) -> str | None:
    """Extract an Amazon Music track ASIN from an Amazon Music URL."""
    text = text.strip()
    for pattern in (
        r"music\.amazon\.[a-z.]+/tracks/([A-Z0-9]{10,})",
        r"[?&]trackAsin=([A-Z0-9]{10,})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# TOTP helper (RFC 6238)
# ---------------------------------------------------------------------------

def _totp(secret_b32: str) -> str:
    """Return a 6-digit TOTP code from a base-32 secret."""
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


# ---------------------------------------------------------------------------
# Spotify access token
# ---------------------------------------------------------------------------
_token_cache: dict = {}

_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en",
    "Referer": "https://open.spotify.com/",
    "Origin": "https://open.spotify.com",
}


def _fetch_anon_token() -> tuple[str, float]:
    """Get an anonymous Spotify access token from the web-player endpoint."""
    params: dict = {"reason": "transport", "productType": "web_player"}
    if SPOTIFY_TOTP_SECRET:
        ts = int(time.time() * 1000)
        params.update({"totp": _totp(SPOTIFY_TOTP_SECRET), "totpVer": "5", "ts": str(ts)})
    resp = requests.get(
        "https://open.spotify.com/get_access_token",
        params=params,
        headers={**_HEADERS_BASE, "app-platform": "WebPlayer"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["accessToken"]
    expires = (data.get("accessTokenExpirationTimestampMs") or 0) / 1000
    return token, expires or time.time() + 3600


def _fetch_cc_token() -> tuple[str, float]:
    """Get a Spotify token via client-credentials grant."""
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], time.time() + data.get("expires_in", 3600)


def get_token() -> str:
    """Return a valid Spotify Bearer token (cached, auto-refreshed)."""
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["token"]

    # 1. Anonymous web-player token
    try:
        token, expires = _fetch_anon_token()
        _token_cache.update({"token": token, "expires_at": expires})
        log.debug("spotify anon token OK (expires %s)", time.ctime(expires))
        return token
    except Exception as exc:
        log.warning("anon token failed: %s", exc)

    # 2. Client-credentials fallback
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            token, expires = _fetch_cc_token()
            _token_cache.update({"token": token, "expires_at": expires})
            log.debug("spotify CC token OK")
            return token
        except Exception as exc:
            log.error("CC token also failed: %s", exc)

    raise RuntimeError(
        "Cannot get Spotify token. "
        "Set SPOTIFY_TOTP_SECRET or SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET."
    )


# ---------------------------------------------------------------------------
# Metadata fetching
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    return {**_HEADERS_BASE, "Authorization": f"Bearer {get_token()}"}


def _fetch_internal_meta(track_id: str) -> dict:
    """Fetch track metadata from Spotify's internal spclient API (JSON)."""
    gid = track_id_to_gid(track_id)
    url = f"https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_public_meta(track_id: str) -> dict:
    """Fetch track metadata from Spotify's public REST API."""
    url = f"https://api.spotify.com/v1/tracks/{track_id}"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_internal(meta: dict) -> dict:
    name = meta.get("name", "")
    artists = [a.get("name", "") for a in meta.get("artist", [])]

    album = meta.get("album", {})
    album_name = album.get("name", "")

    # Cover art — largest available image
    cover_url = ""
    images = album.get("cover_group", {}).get("image", [])
    if images:
        best = max(images, key=lambda x: x.get("width", 0))
        fid = best.get("file_id", "")
        if fid:
            # file_id is base16 hex; some builds encode it as base64
            cover_url = f"https://i.scdn.co/image/{fid}"

    # Release date
    date = album.get("date", {})
    if isinstance(date, dict):
        y = str(date.get("year", ""))
        mo = date.get("month")
        d = date.get("day")
        release_date = f"{y}-{int(mo):02d}-{int(d):02d}" if mo and d else y
    else:
        release_date = str(date)

    # ISRC
    isrc = None
    for eid in meta.get("external_id", []):
        if eid.get("type") == "isrc":
            isrc = eid.get("id")
            break

    return {
        "title": name,
        "artists": artists,
        "album": album_name,
        "release_date": release_date,
        "cover_url": cover_url,
        "track_number": meta.get("number", 1),
        "disc_number": meta.get("disc_number", 1),
        "isrc": isrc,
    }


def _parse_public(meta: dict) -> dict:
    artists = [a["name"] for a in meta.get("artists", [])]
    album = meta.get("album", {})
    images = album.get("images", [])
    cover_url = images[0]["url"] if images else ""
    return {
        "title": meta.get("name", ""),
        "artists": artists,
        "album": album.get("name", ""),
        "release_date": album.get("release_date", ""),
        "cover_url": cover_url,
        "track_number": meta.get("track_number", 1),
        "disc_number": meta.get("disc_number", 1),
        "isrc": meta.get("external_ids", {}).get("isrc"),
    }


def _isrc_soundplate(track_id: str) -> str | None:
    """Last-resort ISRC lookup via Soundplate."""
    try:
        resp = requests.get(
            "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php",
            params={"q": f"https://open.spotify.com/track/{track_id}"},
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            return data.get("isrc") or (data.get("data") or {}).get("isrc")
    except Exception as exc:
        log.warning("soundplate fallback: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Deezer resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Qobuz resolution
# ---------------------------------------------------------------------------

def _resolve_qobuz_by_isrc(isrc: str) -> dict | None:
    """Return the first Qobuz track dict matching the ISRC, or None."""
    if not QOBUZ_APP_ID:
        return None
    try:
        resp = requests.get(
            "https://www.qobuz.com/api.json/0.2/track/search",
            params={"query": isrc, "limit": "5", "app_id": QOBUZ_APP_ID},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if resp.ok:
            data = resp.json()
            tracks = (data.get("tracks") or {}).get("items") or []
            for t in tracks:
                if (t.get("isrc") or "").upper() == isrc.upper():
                    return t
    except Exception as exc:
        log.warning("qobuz ISRC lookup: %s", exc)
    return None


def _get_qobuz_track(track_id: str) -> dict | None:
    """Fetch a Qobuz track by its numeric ID."""
    if not QOBUZ_APP_ID:
        return None
    try:
        resp = requests.get(
            "https://www.qobuz.com/api.json/0.2/track/get",
            params={"track_id": track_id, "app_id": QOBUZ_APP_ID},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if resp.ok:
            data = resp.json()
            if data.get("id") and not data.get("message"):
                return data
    except Exception as exc:
        log.warning("qobuz track get: %s", exc)
    return None


def _parse_qobuz_track(data: dict) -> dict:
    """Convert a Qobuz track API dict to the standard info dict."""
    album = data.get("album") or {}
    images = album.get("image") or {}
    cover_url = (
        images.get("large") or images.get("small") or
        album.get("cover_big") or album.get("cover") or ""
    )
    return {
        "title": data.get("title", ""),
        "artists": [data.get("performer", {}).get("name", "")
                    or data.get("artist", {}).get("name", "")],
        "album": album.get("title", ""),
        "release_date": album.get("release_date_original") or album.get("release_date_stream") or "",
        "cover_url": cover_url,
        "track_number": data.get("track_number", 1),
        "disc_number": data.get("media_number", 1),
        "isrc": data.get("isrc"),
    }


# ---------------------------------------------------------------------------
# Odesli / song.link cross-platform resolution (no auth needed)
# ---------------------------------------------------------------------------

def _resolve_via_odesli(track_url: str) -> dict:
    """
    Resolve a track URL to all platform links via the Odesli / song.link API.
    Returns a dict of {deezer_url, qobuz_url, tidal_url, amazon_url} (keys absent when
    the platform was not found).  No API key required.
    """
    try:
        resp = requests.get(
            "https://api.song.link/v1-alpha.1/links",
            params={"url": track_url, "userCountry": "US"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if not resp.ok:
            return {}
        data = resp.json()
        links = data.get("linksByPlatform") or {}
        result: dict = {}
        if "deezer" in links:
            result["deezer_url"] = links["deezer"]["url"]
        if "tidal" in links:
            result["tidal_url"] = links["tidal"]["url"]
        if "qobuz" in links:
            result["qobuz_url"] = links["qobuz"]["url"]
        if "amazonMusic" in links:
            result["amazon_url"] = links["amazonMusic"]["url"]
        log.debug("odesli resolved: %s", list(result.keys()))
        return result
    except Exception as exc:
        log.warning("odesli resolve: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Songstats cross-platform resolution (scrape HTML, no auth)
# ---------------------------------------------------------------------------

def _resolve_via_songstats(isrc: str) -> dict:
    """
    Scrape Songstats for a given ISRC and return platform URLs found in the
    structured-data (application/ld+json sameAs blocks).  No auth required.
    Returns a dict with any of: deezer_url, tidal_url, amazon_url.
    """
    try:
        resp = requests.get(
            f"https://songstats.com/{isrc}",
            params={"ref": "ISRCFinder"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            timeout=15,
        )
        if not resp.ok:
            return {}
        html = resp.text
        result: dict = {}
        # Extract all JSON-LD blocks
        for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
            try:
                obj = json.loads(block)
                same_as = obj.get("sameAs") or []
                if isinstance(same_as, str):
                    same_as = [same_as]
                for u in same_as:
                    try:
                        from urllib.parse import urlparse as _urlparse
                        host = _urlparse(u).netloc.lower()
                    except Exception:
                        continue
                    if (host == "tidal.com" or host.endswith(".tidal.com")) and "tidal_url" not in result:
                        result["tidal_url"] = u
                    elif (host == "deezer.com" or host.endswith(".deezer.com")) and "deezer_url" not in result:
                        result["deezer_url"] = u
                    elif (host == "music.amazon.com" or host.endswith(".music.amazon.com")) and "amazon_url" not in result:
                        result["amazon_url"] = u
            except Exception:
                pass
        log.debug("songstats resolved: %s", list(result.keys()))
        return result
    except Exception as exc:
        log.warning("songstats resolve: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Tidal resolution
# ---------------------------------------------------------------------------

_TIDAL_API_BASE = "https://api.tidal.com/v1"
_TIDAL_COUNTRY  = "US"


def _tidal_headers() -> dict:
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if TIDAL_TOKEN:
        h["X-Tidal-Token"] = TIDAL_TOKEN
    return h


def _resolve_tidal_by_isrc(isrc: str) -> dict | None:
    """Return the first Tidal track dict matching the ISRC, or None."""
    if not TIDAL_TOKEN:
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
    """Fetch a Tidal track by its numeric ID."""
    if not TIDAL_TOKEN:
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


def _parse_tidal_track(data: dict) -> dict:
    """Convert a Tidal track API dict to the standard info dict."""
    album = data.get("album") or {}
    # Cover art: https://resources.tidal.com/images/<uuid-with-dashes>/640x640.jpg
    cover_id = album.get("cover", "").replace("-", "/")
    cover_url = f"https://resources.tidal.com/images/{cover_id}/640x640.jpg" if cover_id else ""
    release_date = album.get("releaseDate") or ""
    artists = [a.get("name", "") for a in (data.get("artists") or [])]
    if not artists and data.get("artist"):
        artists = [data["artist"].get("name", "")]
    return {
        "title": data.get("title", ""),
        "artists": artists,
        "album": album.get("title", ""),
        "release_date": release_date,
        "cover_url": cover_url,
        "track_number": data.get("trackNumber", 1),
        "disc_number": data.get("volumeNumber", 1),
        "isrc": data.get("isrc"),
    }


# ---------------------------------------------------------------------------
# Public entry point: get_track_info (Spotify → ISRC → multi-platform)
# ---------------------------------------------------------------------------

def _resolve_all_platforms(info: dict) -> dict:
    """
    Given an info dict that already has an ISRC, resolve it on Deezer, Qobuz,
    Tidal, and Amazon Music and add the results as extra keys.

    Resolution chain (each step fills in gaps left by previous steps):
      1. Deezer public ISRC API (always)
      2. Qobuz ISRC search (if QOBUZ_APP_ID set)
      3. Tidal ISRC API (if TIDAL_TOKEN set)
      4. Odesli / song.link API (no auth) — fills in any remaining gaps
      5. Songstats scrape (no auth) — last-resort Tidal/Amazon fallback
    """
    isrc = info.get("isrc") or ""

    info.update({
        "deezer_id": None, "deezer_url": None, "deezer_preview_url": None,
        "qobuz_id": None, "qobuz_url": None,
        "qobuz_bit_depth": None, "qobuz_sample_rate": None,
        "tidal_id": None, "tidal_url": None,
        "amazon_url": None,
    })

    if not isrc:
        return info

    # ── 1. Deezer ──────────────────────────────────────────────────────────
    dz = _resolve_deezer(isrc)
    if dz:
        info["deezer_id"]          = dz["id"]
        info["deezer_url"]         = dz.get("link", f"https://www.deezer.com/track/{dz['id']}")
        info["deezer_preview_url"] = dz.get("preview")
        if not info.get("title"):
            info["title"] = dz.get("title", "")
        if not info.get("artists"):
            info["artists"] = [dz.get("artist", {}).get("name", "")]
        if not info.get("album"):
            info["album"] = dz.get("album", {}).get("title", "")
        if not info.get("cover_url"):
            info["cover_url"] = (
                dz.get("album", {}).get("cover_xl") or
                dz.get("album", {}).get("cover_big") or ""
            )
        log.debug("deezer resolved: id=%s", dz["id"])

    # ── 2. Qobuz ───────────────────────────────────────────────────────────
    qz = _resolve_qobuz_by_isrc(isrc)
    if qz:
        info["qobuz_id"]          = qz["id"]
        info["qobuz_url"]         = f"https://open.qobuz.com/track/{qz['id']}"
        info["qobuz_bit_depth"]   = qz.get("maximum_bit_depth") or qz.get("bit_depth") or 16
        info["qobuz_sample_rate"] = qz.get("maximum_sampling_rate") or qz.get("sampling_rate") or 44100
        log.debug("qobuz resolved: id=%s bd=%s sr=%s",
                  qz["id"], info["qobuz_bit_depth"], info["qobuz_sample_rate"])

    # ── 3. Tidal ───────────────────────────────────────────────────────────
    td = _resolve_tidal_by_isrc(isrc)
    if td:
        info["tidal_id"]  = td["id"]
        info["tidal_url"] = f"https://tidal.com/browse/track/{td['id']}"
        log.debug("tidal resolved: id=%s", td["id"])

    # ── 4. Odesli — fills missing platform URLs (no auth) ─────────────────
    # Build an input URL for Odesli: prefer a Deezer URL we already have,
    # otherwise synthesise a Spotify one if we have a track_id.
    odesli_input = (
        info.get("deezer_url") or
        (f"https://open.spotify.com/track/{info['track_id']}" if info.get("track_id") else None)
    )
    if odesli_input and (not info["tidal_url"] or not info["deezer_url"] or not info["qobuz_url"]):
        od = _resolve_via_odesli(odesli_input)
        if od.get("deezer_url") and not info["deezer_url"]:
            info["deezer_url"] = od["deezer_url"]
        if od.get("qobuz_url") and not info["qobuz_url"]:
            info["qobuz_url"] = od["qobuz_url"]
        if od.get("tidal_url") and not info["tidal_url"]:
            info["tidal_url"] = od["tidal_url"]
        if od.get("amazon_url") and not info["amazon_url"]:
            info["amazon_url"] = od["amazon_url"]

    # ── 5. Songstats — last-resort (no auth) ──────────────────────────────
    if isrc and (not info["tidal_url"] or not info["amazon_url"]):
        sg = _resolve_via_songstats(isrc)
        if sg.get("tidal_url") and not info["tidal_url"]:
            info["tidal_url"] = sg["tidal_url"]
        if sg.get("deezer_url") and not info["deezer_url"]:
            info["deezer_url"] = sg["deezer_url"]
        if sg.get("amazon_url") and not info["amazon_url"]:
            info["amazon_url"] = sg["amazon_url"]

    return info


def get_track_info(track_id: str) -> dict:
    """
    Fetch Spotify track metadata and resolve ISRC on Deezer / Qobuz / Tidal.

    Returns a dict with:
      title, artists (list), album, release_date, cover_url,
      track_number, disc_number, isrc,
      deezer_id, deezer_url, deezer_preview_url,
      qobuz_id, qobuz_url,
      tidal_id, tidal_url
    """
    info: dict = {}

    # --- Phase 1: Spotify metadata ---
    try:
        raw = _fetch_internal_meta(track_id)
        info = _parse_internal(raw)
        log.debug("internal meta OK  track=%s  title=%r", track_id, info.get("title"))
    except Exception as exc:
        log.warning("internal meta failed (%s) — trying public API", exc)
        try:
            raw = _fetch_public_meta(track_id)
            info = _parse_public(raw)
            log.debug("public meta OK  track=%s  title=%r", track_id, info.get("title"))
        except Exception as exc2:
            log.error("public meta also failed: %s", exc2)
            info = {
                "title": "", "artists": [], "album": "",
                "release_date": "", "cover_url": "",
                "track_number": 1, "disc_number": 1, "isrc": None,
            }

    info["track_id"] = track_id

    # ISRC via Soundplate if still missing
    if not info.get("isrc"):
        info["isrc"] = _isrc_soundplate(track_id)

    # --- Phase 2: multi-platform resolution ---
    return _resolve_all_platforms(info)


# ---------------------------------------------------------------------------
# Public entry point: get_tidal_track_info
# ---------------------------------------------------------------------------

def get_tidal_track_info(track_id: str) -> dict:
    """
    Fetch Tidal track metadata and resolve ISRC on Deezer / Qobuz.
    Raises RuntimeError if TIDAL_TOKEN is not set.
    """
    if not TIDAL_TOKEN:
        raise RuntimeError(
            "TIDAL_TOKEN env var is required to look up Tidal tracks."
        )

    data = _get_tidal_track(track_id)
    if not data:
        raise RuntimeError(f"Tidal API returned no data for track {track_id!r}")

    info = _parse_tidal_track(data)
    info["track_id"]  = None
    info["tidal_id"]  = track_id
    info["tidal_url"] = f"https://tidal.com/browse/track/{track_id}"

    return _resolve_all_platforms(info)


# ---------------------------------------------------------------------------
# Public entry point: get_qobuz_track_info
# ---------------------------------------------------------------------------

def get_qobuz_track_info(track_id: str) -> dict:
    """
    Fetch Qobuz track metadata and resolve ISRC on Deezer / Tidal.
    Raises RuntimeError if QOBUZ_APP_ID is not set.
    """
    if not QOBUZ_APP_ID:
        raise RuntimeError(
            "QOBUZ_APP_ID env var is required to look up Qobuz tracks."
        )

    data = _get_qobuz_track(track_id)
    if not data:
        raise RuntimeError(f"Qobuz API returned no data for track {track_id!r}")

    info = _parse_qobuz_track(data)
    info["track_id"]  = None
    info["qobuz_id"]  = track_id
    info["qobuz_url"] = f"https://open.qobuz.com/track/{track_id}"

    return _resolve_all_platforms(info)


# ---------------------------------------------------------------------------
# Public entry point: get_amazon_track_info
# ---------------------------------------------------------------------------

def get_amazon_track_info(track_id: str, ytdlp_bin: str) -> dict:
    """
    Extract Amazon Music track metadata via yt-dlp and resolve ISRC on
    Deezer / Qobuz / Tidal.  Falls back to minimal info if extraction fails.
    """
    url = f"https://music.amazon.com/tracks/{track_id}"
    info: dict = {
        "title": "", "artists": [], "album": "",
        "release_date": "", "cover_url": "",
        "track_number": 1, "disc_number": 1, "isrc": None,
        "track_id": None,
        "amazon_id": track_id,
        "amazon_url": url,
    }

    try:
        result = subprocess.run(
            [ytdlp_bin, "--dump-json", "--quiet", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            info["title"]        = data.get("title") or ""
            info["album"]        = data.get("album") or ""
            info["release_date"] = data.get("release_date") or data.get("upload_date") or ""
            info["cover_url"]    = data.get("thumbnail") or ""
            info["isrc"]         = data.get("isrc") or None
            artist = data.get("artist") or data.get("uploader") or ""
            if artist:
                info["artists"] = [artist]
            log.debug("amazon yt-dlp json OK for %s", track_id)
    except Exception as exc:
        log.warning("amazon yt-dlp json failed: %s", exc)

    return _resolve_all_platforms(info)


# ---------------------------------------------------------------------------
# Metadata tagging
# ---------------------------------------------------------------------------

def _safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(". ")[:200]


def embed_metadata(filepath: Path, info: dict) -> None:
    """Embed ID3 (MP3) or Vorbis/FLAC tags and cover art using mutagen."""
    try:
        from mutagen.id3 import (
            ID3, ID3NoHeaderError,
            TIT2, TPE1, TALB, TDRC, TRCK, TPOS, APIC, TSRC,
        )
        from mutagen.flac import FLAC, Picture
    except ImportError:
        log.warning("mutagen not installed — skipping tag embedding")
        return

    # Download cover art once
    cover_data: bytes | None = None
    if info.get("cover_url"):
        try:
            req = urllib.request.Request(info["cover_url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                cover_data = r.read()
        except Exception as exc:
            log.warning("cover art download failed: %s", exc)

    ext = filepath.suffix.lower()

    if ext == ".mp3":
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()

        tags.add(TIT2(encoding=3, text=info.get("title", "")))
        tags.add(TPE1(encoding=3, text=", ".join(info.get("artists", []))))
        tags.add(TALB(encoding=3, text=info.get("album", "")))
        tags.add(TDRC(encoding=3, text=str(info.get("release_date", ""))))
        tags.add(TRCK(encoding=3, text=str(info.get("track_number", 1))))
        tags.add(TPOS(encoding=3, text=str(info.get("disc_number", 1))))
        if info.get("isrc"):
            tags.add(TSRC(encoding=3, text=info["isrc"]))
        if cover_data:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))
        tags.save(str(filepath))
        log.debug("ID3 tags written to %s", filepath.name)

    elif ext == ".flac":
        audio = FLAC(str(filepath))
        audio["title"]       = info.get("title", "")
        audio["artist"]      = ", ".join(info.get("artists", []))
        audio["album"]       = info.get("album", "")
        audio["date"]        = str(info.get("release_date", ""))
        audio["tracknumber"] = str(info.get("track_number", 1))
        audio["discnumber"]  = str(info.get("disc_number", 1))
        if info.get("isrc"):
            audio["isrc"] = info["isrc"]
        if cover_data:
            pic = Picture()
            pic.type = 3  # cover front
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        log.debug("FLAC tags written to %s", filepath.name)


# ---------------------------------------------------------------------------
# Quality / platform menu builder
# ---------------------------------------------------------------------------

# Quality tier constants
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


# ---------------------------------------------------------------------------
# Qobuz no-auth stream download (proxy APIs)
# ---------------------------------------------------------------------------

# Proxy endpoints that return a signed Qobuz stream URL.
# Each endpoint accepts trackId (Qobuz numeric ID) and quality level.
_QOBUZ_STREAM_PROXIES = [
    "https://dab.yeet.su/api/stream?trackId={id}&quality={q}",
    "https://dabmusic.xyz/api/stream?trackId={id}&quality={q}",
    "https://qobuz.spotbye.qzz.io/api/track/{id}?quality={q}",
]

# Quality level fallback chains
# 27 = Hi-Res Max (24-bit up to 192 kHz)  /  7 = 24-bit Standard  /  6 = 16-bit Lossless CD
_QOBUZ_QUALITY_CHAIN = {
    QUALITY_FLAC_HI: [27,  # Hi-Res Max
                      7,   # 24-bit Standard
                      6],  # 16-bit Lossless CD
    QUALITY_FLAC_CD: [6,   # 16-bit Lossless CD
                      7],  # 24-bit Standard (fallback)
    QUALITY_MP3:     [],   # Qobuz not used for MP3
}


def _get_qobuz_stream_url(track_id: str, quality_num: int) -> str | None:
    """
    Try each proxy API in order and return the first signed stream URL found.
    Returns *None* if all proxies fail for this (track_id, quality_num) pair.
    """
    for template in _QOBUZ_STREAM_PROXIES:
        url = template.format(id=track_id, q=quality_num)
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
                allow_redirects=True,
            )
            if not resp.ok:
                log.debug("qobuz proxy %s → HTTP %d", url, resp.status_code)
                continue

            ct = resp.headers.get("content-type", "")
            # JSON response — look for a URL field
            if "json" in ct:
                try:
                    data = resp.json()
                    stream_url = (
                        data.get("url")
                        or data.get("stream_url")
                        or data.get("download_url")
                        or data.get("link")
                    )
                    if stream_url and str(stream_url).startswith("http"):
                        log.debug("qobuz stream via %s (json)", url)
                        return str(stream_url)
                except Exception:
                    pass

            # Plain-text URL
            text = resp.text.strip()
            if text.startswith("http"):
                log.debug("qobuz stream via %s (plain)", url)
                return text

            # The proxy might have redirected to the actual CDN URL
            if resp.url != url:
                try:
                    from urllib.parse import urlparse as _urlparse
                    redir_host = _urlparse(resp.url).netloc.lower()
                except Exception:
                    redir_host = ""
                if (
                    redir_host == "storage.googleapis.com"
                    or redir_host.endswith(".storage.googleapis.com")
                    or "qobuz" in redir_host
                    or resp.url.endswith(".flac")
                ):
                    log.debug("qobuz stream via %s (redirect)", url)
                    return resp.url

        except Exception as exc:
            log.debug("qobuz proxy %s error: %s", url, exc)

    return None


def _download_qobuz_stream_sync(stream_url: str, dest_path: Path) -> None:
    """Download a FLAC file from a direct stream URL (blocking, call in executor)."""
    resp = requests.get(
        stream_url,
        headers={"User-Agent": "Mozilla/5.0"},
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    with open(dest_path, "wb") as fout:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                fout.write(chunk)
    log.info("qobuz download complete: %s (%.1f MB)",
             dest_path.name, dest_path.stat().st_size / (1024 * 1024))


async def _download_qobuz(
    track_id: str, quality_tier: str,
    download_dir: Path, filename_stem: str
) -> Path:
    """
    Obtain a signed Qobuz stream URL via proxy APIs and download the FLAC.
    Tries quality levels from *_QOBUZ_QUALITY_CHAIN[quality_tier]* in order.
    Raises RuntimeError if no stream URL can be obtained.
    """
    loop = asyncio.get_event_loop()
    quality_nums = _QOBUZ_QUALITY_CHAIN.get(quality_tier, [6])

    stream_url: str | None = None
    used_quality: int | None = None
    for qnum in quality_nums:
        stream_url = await loop.run_in_executor(
            None, _get_qobuz_stream_url, str(track_id), qnum
        )
        if stream_url:
            used_quality = qnum
            break

    if not stream_url:
        raise RuntimeError(
            f"Could not obtain a Qobuz stream URL for track {track_id!r} "
            f"(tried quality chain {quality_nums})"
        )

    dest_path = download_dir / f"{filename_stem}.flac"
    # Remove any leftover file from a previous partial attempt
    if dest_path.exists():
        dest_path.unlink()

    log.info("downloading qobuz track %s quality=%s → %s", track_id, used_quality, dest_path.name)
    await loop.run_in_executor(None, _download_qobuz_stream_sync, stream_url, dest_path)
    return dest_path


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

async def download_track(info: dict, download_dir: Path, ytdlp_bin: str) -> Path:
    """
    Download a single track to *download_dir* and embed metadata.
    Returns the Path of the downloaded file.

    Priority:
      1. Qobuz FLAC  — via proxy stream APIs (no credentials required), if qobuz_id available
      2. Deezer FLAC — if DEEZER_ARL set and deezer_url available
      3. YouTube Music MP3 320 k — always available as fallback
    """
    title       = info.get("title", "Unknown")
    artists_str = ", ".join(info.get("artists", ["Unknown"]))
    safe        = _safe_filename(f"{title} - {artists_str}")

    # ── 1. Qobuz via proxy stream API (no credentials needed) ─────────────
    qobuz_id = info.get("qobuz_id")
    if qobuz_id:
        try:
            fp = await _download_qobuz(qobuz_id, QUALITY_FLAC_HI, download_dir, safe)
            try:
                embed_metadata(fp, info)
            except Exception as exc:
                log.warning("metadata embed failed for %s: %s", fp.name, exc)
            return fp
        except Exception as exc:
            log.warning("qobuz proxy download failed, trying Deezer/YTMusic: %s", exc)

    output_tmpl = str(download_dir / f"{safe}.%(ext)s")

    # ── 2. Deezer FLAC ─────────────────────────────────────────────────────
    if DEEZER_ARL and info.get("deezer_url"):
        cmd = [
            ytdlp_bin,
            info["deezer_url"],
            "--extract-audio",
            "--audio-format", "flac",
            "--add-header", f"Cookie: arl={DEEZER_ARL}",
            "-o", output_tmpl,
            "--no-playlist",
            "--quiet", "--no-warnings",
        ]
        source = "Deezer"

    # ── 3. YouTube Music MP3 ───────────────────────────────────────────────
    else:
        search = f"{title} {artists_str}"
        cmd = [
            ytdlp_bin,
            f"ytmsearch1:{search}",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", output_tmpl,
            "--no-playlist",
            "--quiet", "--no-warnings",
        ]
        source = "YouTube Music"

    log.info("downloading via %s: %r", source, title)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        err = stdout.decode(errors="replace")
        raise RuntimeError(f"yt-dlp ({source}) exit {proc.returncode}: {err[:400]}")

    exts = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}
    candidates = sorted(
        (p for p in download_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("yt-dlp reported success but no audio file found")

    fp = candidates[0]
    try:
        embed_metadata(fp, info)
    except Exception as exc:
        log.warning("metadata embed failed for %s: %s", fp.name, exc)

    return fp


def best_source_label(info: dict) -> str:
    """Return a human-readable label for the download source that will be used."""
    if info.get("qobuz_id"):
        return "\U0001f1f6\U0001f1ff Qobuz FLAC"
    if DEEZER_ARL and info.get("deezer_url"):
        return "\U0001f1eb\U0001f1f7 Deezer FLAC"
    return "MP3 320k"


# ---------------------------------------------------------------------------
# Playlist / album support
# ---------------------------------------------------------------------------

def parse_spotify_playlist_id(text: str) -> str | None:
    """Extract a 22-char Spotify playlist ID from a URL, URI, or bare ID."""
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/playlist/([A-Za-z0-9]{22})",
        r"spotify:playlist:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def parse_spotify_album_id(text: str) -> str | None:
    """Extract a 22-char Spotify album ID from a URL, URI, or bare ID."""
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/album/([A-Za-z0-9]{22})",
        r"spotify:album:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def get_spotify_playlist_tracks(playlist_id: str) -> tuple[dict, list]:
    """
    Fetch metadata and all tracks for a Spotify playlist.
    Returns (playlist_info_dict, list_of_track_ids).
    """
    headers = _auth_headers()

    # Playlist name / image
    pl_resp = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        params={"fields": "name,owner,images"},
        timeout=15,
    )
    pl_resp.raise_for_status()
    pl_data = pl_resp.json()

    playlist_info = {
        "name": pl_data.get("name", "playlist"),
        "owner": (pl_data.get("owner") or {}).get("display_name", ""),
        "cover_url": ((pl_data.get("images") or [{}])[0]).get("url", ""),
    }

    # Paginate tracks
    track_ids: list = []
    url: str | None = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params: dict = {
        "limit": 100,
        "fields": "items(track(id)),next",
    }
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            t = item.get("track")
            if t and t.get("id"):
                track_ids.append(t["id"])
        url = data.get("next")
        params = {}

    log.info("playlist %s: %d tracks", playlist_id, len(track_ids))
    return playlist_info, track_ids


def get_spotify_album_tracks(album_id: str) -> tuple[dict, list]:
    """
    Fetch metadata and all tracks for a Spotify album.
    Returns (album_info_dict, list_of_track_ids).
    """
    headers = _auth_headers()

    al_resp = requests.get(
        f"https://api.spotify.com/v1/albums/{album_id}",
        headers=headers,
        timeout=15,
    )
    al_resp.raise_for_status()
    al_data = al_resp.json()

    album_info = {
        "name": al_data.get("name", "album"),
        "artists": [a["name"] for a in al_data.get("artists", [])],
        "release_date": al_data.get("release_date", ""),
        "cover_url": ((al_data.get("images") or [{}])[0]).get("url", ""),
        "total_tracks": al_data.get("total_tracks", 0),
    }

    track_ids: list = []
    url: str | None = f"https://api.spotify.com/v1/albums/{album_id}/tracks"
    params: dict = {"limit": 50}
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for t in data.get("items", []):
            if t.get("id"):
                track_ids.append(t["id"])
        url = data.get("next")
        params = {}

    log.info("album %s: %d tracks", album_id, len(track_ids))
    return album_info, track_ids



def build_platform_choices(info: dict, quality: str) -> list:
    """
    Build a list of download-source choices for a resolved track given the
    user's requested quality tier.

    Qobuz is available whenever the track has a qobuz_id (no credentials required
    — downloads use the public proxy stream APIs).

    Each choice dict mirrors the structure used by the YouTube quality menu:
      label      – display string shown to the user
      source     – "qobuz" | "deezer" | "ytmusic"
      quality    – one of the QUALITY_* constants (actual quality of this source)
      audio_only – True
      out_ext    – "flac" | "mp3"
      url        – source URL (None for ytmusic fallback)
    """
    choices: list = []
    want_flac  = quality in (QUALITY_FLAC_CD, QUALITY_FLAC_HI)
    want_hires = quality == QUALITY_FLAC_HI

    # ── Qobuz — no credentials required; uses proxy stream APIs ───────────
    qobuz_id = info.get("qobuz_id")
    if qobuz_id and want_flac:
        bd = info.get("qobuz_bit_depth") or 16
        sr = info.get("qobuz_sample_rate") or 44100
        sr_khz = sr / 1000

        if want_hires and bd and int(bd) > 16:
            choices.append({
                "label":      f"\U0001f1f6\U0001f1ff Qobuz FLAC Hi-Res \u2014 {bd}-bit / {sr_khz:.0f} kHz",
                "source":     "qobuz",
                "quality":    QUALITY_FLAC_HI,
                "audio_only": True,
                "out_ext":    "flac",
                "url":        info.get("qobuz_url"),
                "qobuz_id":   qobuz_id,
            })
        # Always add CD quality (either as main option or fallback note)
        note = ""
        if want_hires and not (bd and int(bd) > 16):
            note = " \u26a0\ufe0f Hi-Res not available for this track"
        choices.append({
            "label":      f"\U0001f1f6\U0001f1ff Qobuz FLAC CD \u2014 16-bit / 44.1 kHz{note}",
            "source":     "qobuz",
            "quality":    QUALITY_FLAC_CD,
            "audio_only": True,
            "out_ext":    "flac",
            "url":        info.get("qobuz_url"),
            "qobuz_id":   qobuz_id,
        })

    # ── Deezer — requires DEEZER_ARL ───────────────────────────────────────
    if DEEZER_ARL and info.get("deezer_url") and want_flac:
        choices.append({
            "label":      "\U0001f1eb\U0001f1f7 Deezer FLAC CD \u2014 16-bit / 44.1 kHz",
            "source":     "deezer",
            "quality":    QUALITY_FLAC_CD,
            "audio_only": True,
            "out_ext":    "flac",
            "url":        info["deezer_url"],
        })

    # ── YouTube Music MP3 — always available ───────────────────────────────
    title       = info.get("title", "")
    artists_str = ", ".join(info.get("artists") or [])
    choices.append({
        "label":      "\U0001f3b5 YouTube Music MP3 320k",
        "source":     "ytmusic",
        "quality":    QUALITY_MP3,
        "audio_only": True,
        "out_ext":    "mp3",
        "url":        None,
        "search":     f"{title} {artists_str}".strip(),
    })

    return choices


# ---------------------------------------------------------------------------
# Download from a specific platform choice
# ---------------------------------------------------------------------------

async def download_track_from_choice(
    info: dict, choice: dict, download_dir: Path, ytdlp_bin: str
) -> Path:
    """
    Download a track using a pre-selected platform+quality *choice* dict
    (as produced by build_platform_choices).  Embeds metadata afterwards.

    Qobuz:    uses the no-auth proxy stream APIs (_download_qobuz).
    Deezer:   uses yt-dlp with DEEZER_ARL cookie.
    ytmusic:  uses yt-dlp ytmsearch.
    """
    title       = info.get("title", "Unknown")
    artists_str = ", ".join(info.get("artists", ["Unknown"]))
    safe        = _safe_filename("{} - {}".format(title, artists_str))

    source = choice.get("source", "ytmusic")
    log.info("download_track_from_choice source=%s title=%r", source, title)

    # ── Qobuz — proxy stream APIs (no credentials) ─────────────────────────
    if source == "qobuz":
        qobuz_id = choice.get("qobuz_id") or info.get("qobuz_id")
        if not qobuz_id:
            # Try to extract ID from URL
            url_str = choice.get("url") or info.get("qobuz_url") or ""
            m = re.search(r"qobuz\.com/track/(\d+)", url_str)
            if m:
                qobuz_id = m.group(1)
        if not qobuz_id:
            raise RuntimeError("Qobuz track ID not available in choice or info dict")

        quality_tier = choice.get("quality", QUALITY_FLAC_CD)
        fp = await _download_qobuz(qobuz_id, quality_tier, download_dir, safe)
        try:
            embed_metadata(fp, info)
        except Exception as exc:
            log.warning("metadata embed failed for %s: %s", fp.name, exc)
        return fp

    output_tmpl = str(download_dir / "{}.%(ext)s".format(safe))

    # ── Deezer — yt-dlp + ARL cookie ──────────────────────────────────────
    if source == "deezer" and DEEZER_ARL:
        cmd = [
            ytdlp_bin,
            choice["url"],
            "--extract-audio", "--audio-format", "flac",
            "--add-header", "Cookie: arl={}".format(DEEZER_ARL),
            "-o", output_tmpl,
            "--no-playlist", "--quiet", "--no-warnings",
        ]
    # ── YouTube Music MP3 fallback ─────────────────────────────────────────
    else:
        search = choice.get("search") or "{} {}".format(title, artists_str)
        cmd = [
            ytdlp_bin,
            "ytmsearch1:{}".format(search),
            "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", output_tmpl,
            "--no-playlist", "--quiet", "--no-warnings",
        ]
        source = "YouTube Music"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        err = stdout.decode(errors="replace")
        raise RuntimeError("yt-dlp ({}) exit {}: {}".format(source, proc.returncode, err[:400]))

    exts = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"}
    candidates = sorted(
        (p for p in download_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("yt-dlp reported success but no audio file found")

    fp = candidates[0]
    try:
        embed_metadata(fp, info)
    except Exception as exc:
        log.warning("metadata embed failed for %s: %s", fp.name, exc)

    return fp
