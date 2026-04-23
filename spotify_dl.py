# -*- coding: utf-8 -*-
"""
SpotiFLAC-style Spotify track downloader.

Phase 1 — Retrieve track metadata + ISRC from Spotify (anonymous token via TOTP).
Phase 2 — Resolve ISRC to Deezer.
Phase 3 — Download via yt-dlp (Deezer with ARL → FLAC, or YouTube Music → MP3 320 k).
           Embed full Spotify metadata with mutagen.

Environment variables (all optional):
  SPOTIFY_TOTP_SECRET     Base-32 TOTP secret for anonymous Spotify token.
  SPOTIFY_CLIENT_ID       } Spotify app credentials; used as fallback
  SPOTIFY_CLIENT_SECRET   }   if the anonymous token endpoint fails.
  DEEZER_ARL              Deezer cookie — enables lossless FLAC download.
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
# Public entry point: get_track_info
# ---------------------------------------------------------------------------

def get_track_info(track_id: str) -> dict:
    """
    Return a dict with:
      title, artists (list), album, release_date, cover_url,
      track_number, disc_number, isrc,
      deezer_id, deezer_url, deezer_preview_url
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

    # --- Phase 2: Deezer resolution ---
    info.update({"deezer_id": None, "deezer_url": None, "deezer_preview_url": None})

    if info.get("isrc"):
        dz = _resolve_deezer(info["isrc"])
        if dz:
            info["deezer_id"] = dz["id"]
            info["deezer_url"] = dz.get("link", f"https://www.deezer.com/track/{dz['id']}")
            info["deezer_preview_url"] = dz.get("preview")
            # Fill in any gaps from Deezer
            if not info["title"]:
                info["title"] = dz.get("title", "")
            if not info["artists"]:
                info["artists"] = [dz.get("artist", {}).get("name", "")]
            if not info["album"]:
                info["album"] = dz.get("album", {}).get("title", "")
            if not info["cover_url"]:
                info["cover_url"] = (
                    dz.get("album", {}).get("cover_xl")
                    or dz.get("album", {}).get("cover_big")
                    or ""
                )
            log.debug("deezer resolved: id=%s  url=%s", dz["id"], info["deezer_url"])

    return info


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
# Download
# ---------------------------------------------------------------------------

async def download_track(info: dict, download_dir: Path, ytdlp_bin: str) -> Path:
    """
    Download a single track to *download_dir* and embed Spotify metadata.
    Returns the Path of the downloaded file.

    Strategy:
      - If DEEZER_ARL is set and a Deezer URL is known → download FLAC from Deezer.
      - Otherwise → search YouTube Music (ytmsearch1:) and download MP3 320 k.
    """
    title       = info.get("title", "Unknown")
    artists_str = ", ".join(info.get("artists", ["Unknown"]))
    safe        = _safe_filename(f"{title} - {artists_str}")
    output_tmpl = str(download_dir / f"{safe}.%(ext)s")

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

    # Find the downloaded file
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
