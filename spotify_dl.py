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

Qobuz metadata API credentials are auto-scraped from open.qobuz.com and
cached on disk for 24 hours — no account, email, or API key is needed.

Environment variables (all optional):
  SPOTIFY_CLIENT_ID       } Spotify app credentials; fallback if anon token fails.
  SPOTIFY_CLIENT_SECRET   }
  DEEZER_ARL              Deezer account ARL cookie — enables lossless FLAC.
  TIDAL_TOKEN             Tidal client/OAuth token for metadata lookup.
  QOBUZ_EMAIL             } Optional Qobuz account for authenticated fallback when
  QOBUZ_PASSWORD          }   all proxy APIs fail (port of SpotiFLAC qobuz_api.go).
  TIDAL_ALT_BASES         Comma-separated list of Tidal Alt proxy base URLs to rotate
                          (port of SpotiFLAC backend/tidal_api_list.go).
"""
import asyncio
import base64
import collections
import concurrent.futures
import hashlib
import hmac
import html
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEEZER_ARL            = os.getenv("DEEZER_ARL",            "").strip()
TIDAL_TOKEN           = os.getenv("TIDAL_TOKEN",           "").strip()
# Optional authenticated Qobuz fallback (Gap 8 — port of SpotiFLAC backend/qobuz_api.go:userLogin)
QOBUZ_EMAIL           = os.getenv("QOBUZ_EMAIL",           "").strip()
QOBUZ_PASSWORD        = os.getenv("QOBUZ_PASSWORD",        "").strip()
# Tidal Alt endpoint rotation (Gap 5 — port of SpotiFLAC backend/tidal_api_list.go)
# Override at runtime via TIDAL_ALT_BASES env var (comma-separated list of base URLs).
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

# ---------------------------------------------------------------------------
# Recent-fetch metadata cache (Gap 6 — port of SpotiFLAC backend/recent_fetches.go)
# ---------------------------------------------------------------------------
_TRACK_INFO_CACHE_MAX  = 256   # max entries
_TRACK_INFO_CACHE_TTL  = 600   # seconds (10 min)
_track_info_cache: "collections.OrderedDict[str, tuple[float, dict]]" = collections.OrderedDict()
_track_info_cache_lock = threading.Lock()


def _cache_get_track_info(track_id: str) -> dict | None:
    with _track_info_cache_lock:
        entry = _track_info_cache.get(track_id)
        if entry is None:
            return None
        ts, data = entry
        if time.time() - ts > _TRACK_INFO_CACHE_TTL:
            _track_info_cache.pop(track_id, None)
            return None
        # LRU: move to end
        _track_info_cache.move_to_end(track_id)
        return data


def _cache_set_track_info(track_id: str, info: dict) -> None:
    with _track_info_cache_lock:
        _track_info_cache[track_id] = (time.time(), info)
        _track_info_cache.move_to_end(track_id)
        while len(_track_info_cache) > _TRACK_INFO_CACHE_MAX:
            _track_info_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Download history (Gap 7 — port of SpotiFLAC backend/history.go)
# ---------------------------------------------------------------------------
_DOWNLOAD_HISTORY_PATH = Path(tempfile.gettempdir()) / "tele2rub" / "downloads_history.json"
_download_history_lock = threading.Lock()


def _load_download_history() -> dict:
    try:
        return json.loads(_DOWNLOAD_HISTORY_PATH.read_text())
    except Exception:
        return {}


def _save_download_history(history: dict) -> None:
    try:
        _DOWNLOAD_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DOWNLOAD_HISTORY_PATH.write_text(json.dumps(history, indent=2))
    except Exception as exc:
        log.debug("download history save failed: %s", exc)


def _history_key(track_id: str, source: str, quality: str) -> str:
    return f"{track_id}|{source}|{quality}"


def _check_download_history(track_id: str, source: str, quality: str) -> Path | None:
    """Return path of previously downloaded file if it still exists, else None."""
    try:
        with _download_history_lock:
            history = _load_download_history()
        key = _history_key(track_id, source, quality)
        entry = history.get(key)
        if entry:
            fp = Path(entry)
            if fp.exists() and fp.stat().st_size > 0:
                return fp
    except Exception as exc:
        log.debug("download history check failed: %s", exc)
    return None


def _record_download_history(track_id: str, source: str, quality: str, file_path: Path) -> None:
    """Persist a successful download to history (best-effort, never fatal)."""
    try:
        with _download_history_lock:
            history = _load_download_history()
            history[_history_key(track_id, source, quality)] = str(file_path)
            _save_download_history(history)
    except Exception as exc:
        log.debug("download history record failed: %s", exc)


# ---------------------------------------------------------------------------
# Spotify TOTP — hardcoded secret and version (no env var needed)
# ---------------------------------------------------------------------------
_SPOTIFY_TOTP_SECRET  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
_SPOTIFY_TOTP_VERSION = 61

# Hardcoded fallback in case open.spotify.com A/B test hides appServerConfig.
# Ref: SpotiFLAC issue #14 fix
_SPOTIFY_CLIENT_VERSION_FALLBACK = "1.2.52.442.g55a7e7d3"

# ---------------------------------------------------------------------------
# Qobuz API — auto-scraped credentials (no account needed)
# ---------------------------------------------------------------------------

_QOBUZ_API_BASE          = "https://www.qobuz.com/api.json/0.2"
_QOBUZ_DEFAULT_APP_ID    = "712109809"
_QOBUZ_DEFAULT_APP_SECRET= "589be88e4538daea11f509d29e4a23b1"
_QOBUZ_OPEN_PROBE_URL    = "https://open.qobuz.com/track/1"
_QOBUZ_CREDS_CACHE_TTL   = 24 * 3600  # seconds
_QOBUZ_PROBE_ISRC        = "USUM71703861"
_QOBUZ_UA                = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_qobuz_bundle_re  = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
# More lenient: match app_id and app_secret independently of order/whitespace
# Ref: SpotiFLAC backend/qobuz_api.go
_qobuz_app_id_re     = re.compile(r'"?app_id"?\s*[:=]\s*"?(\d{7,12})"?')
_qobuz_app_secret_re = re.compile(r'"?app_secret"?\s*[:=]\s*"?([a-f0-9]{32})"?')

_qobuz_creds_lock  = threading.Lock()
_qobuz_creds_cache: dict | None = None  # {"app_id", "app_secret", "source", "fetched_at"}


def _qobuz_creds_cache_path() -> Path:
    cache_dir = Path(tempfile.gettempdir()) / "tele2rub"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "qobuz-api-credentials.json"


def _load_qobuz_creds() -> dict | None:
    try:
        data = json.loads(_qobuz_creds_cache_path().read_text())
        if data.get("app_id") and data.get("app_secret"):
            return data
    except Exception:
        pass
    return None


def _save_qobuz_creds(creds: dict) -> None:
    try:
        _qobuz_creds_cache_path().write_text(json.dumps(creds, indent=2))
    except Exception as exc:
        log.warning("failed to write qobuz credentials cache: %s", exc)


def _qobuz_creds_fresh(creds: dict | None) -> bool:
    if not creds or not creds.get("app_id") or not creds.get("app_secret"):
        return False
    return time.time() - creds.get("fetched_at", 0) < _QOBUZ_CREDS_CACHE_TTL


def _scrape_qobuz_open_credentials() -> dict | None:
    """Fetch open.qobuz.com, find the JS bundle, extract app_id + app_secret."""
    try:
        resp = requests.get(
            _QOBUZ_OPEN_PROBE_URL,
            headers={"User-Agent": _QOBUZ_UA},
            timeout=20,
        )
        if not resp.ok:
            log.debug("open.qobuz.com returned %d", resp.status_code)
            return None

        m = _qobuz_bundle_re.search(resp.text)
        if not m:
            log.debug("qobuz open bundle URL not found in HTML")
            return None

        bundle_url = m.group(1)
        if bundle_url.startswith("/"):
            bundle_url = "https://open.qobuz.com" + bundle_url

        bundle_resp = requests.get(
            bundle_url,
            headers={"User-Agent": _QOBUZ_UA},
            timeout=30,
        )
        if not bundle_resp.ok:
            log.debug("qobuz bundle fetch returned %d", bundle_resp.status_code)
            return None

        bundle_text = bundle_resp.text
        m_id  = _qobuz_app_id_re.search(bundle_text)
        m_sec = _qobuz_app_secret_re.search(bundle_text)
        if not m_id or not m_sec:
            log.debug("qobuz app_id/app_secret not found in bundle")
            return None

        creds = {
            "app_id":     m_id.group(1),
            "app_secret": m_sec.group(1),
            "source":     bundle_url,
            "fetched_at": time.time(),
        }
        log.debug("scraped qobuz credentials: app_id=%s from %s", creds["app_id"], bundle_url)
        return creds

    except Exception as exc:
        log.warning("qobuz credential scraping failed: %s", exc)
        return None


def _qobuz_creds_valid(creds: dict | None) -> bool:
    """Probe the track/search endpoint to confirm credentials work."""
    if not creds:
        return False
    try:
        params = _qobuz_signed_params("track/search", {"query": _QOBUZ_PROBE_ISRC, "limit": "1"}, creds)
        resp = requests.get(
            f"{_QOBUZ_API_BASE}/track/search",
            params=params,
            headers={"User-Agent": _QOBUZ_UA, "Accept": "application/json",
                     "X-App-Id": creds["app_id"]},
            timeout=15,
        )
        if not resp.ok:
            return False
        data = resp.json()
        return (data.get("tracks") or {}).get("total", 0) > 0
    except Exception:
        return False


def _get_qobuz_api_credentials(force_refresh: bool = False) -> dict:
    """
    Return valid Qobuz API credentials, auto-scraped from open.qobuz.com.
    Falls back to embedded defaults if scraping fails.  Thread-safe.
    """
    global _qobuz_creds_cache
    with _qobuz_creds_lock:
        if not force_refresh and _qobuz_creds_fresh(_qobuz_creds_cache):
            return _qobuz_creds_cache  # type: ignore[return-value]

        disk = _load_qobuz_creds()
        if not force_refresh and _qobuz_creds_fresh(disk):
            _qobuz_creds_cache = disk
            return disk  # type: ignore[return-value]

        scraped = _scrape_qobuz_open_credentials()
        if scraped and _qobuz_creds_valid(scraped):
            _qobuz_creds_cache = scraped
            _save_qobuz_creds(scraped)
            log.info("qobuz credentials refreshed from open bundle (app_id=%s)", scraped["app_id"])
            return scraped

        if disk:
            log.warning("qobuz credential refresh failed, using cached credentials")
            _qobuz_creds_cache = disk
            return disk

        if _qobuz_creds_cache:
            log.warning("qobuz credential refresh failed, using in-memory credentials")
            return _qobuz_creds_cache

        fallback = {
            "app_id":    _QOBUZ_DEFAULT_APP_ID,
            "app_secret": _QOBUZ_DEFAULT_APP_SECRET,
            "source":    "embedded-default",
            "fetched_at": time.time(),
        }
        _qobuz_creds_cache = fallback
        log.warning("qobuz using embedded fallback credentials (app_id=%s)", fallback["app_id"])
        return fallback


def _qobuz_signed_params(path: str, params: dict, creds: dict) -> dict:
    """
    Build a signed params dict for the Qobuz API (identical algorithm to the Go code).
    Signature = MD5( normalizedPath + sorted(key+value pairs) + timestamp + secret )
    """
    normalized = path.strip("/").replace("/", "")
    timestamp  = str(int(time.time()))
    exclude    = {"app_id", "request_ts", "request_sig"}
    sorted_keys = sorted(k for k in params if k not in exclude)

    payload = normalized
    for k in sorted_keys:
        v = params[k]
        if isinstance(v, (list, tuple)):
            for vi in v:
                payload += k + str(vi)
        else:
            payload += k + str(v)
    payload += timestamp + creds["app_secret"]

    sig = hashlib.md5(payload.encode()).hexdigest()

    out = dict(params)
    out["app_id"]      = creds["app_id"]
    out["request_ts"]  = timestamp
    out["request_sig"] = sig
    return out


def _do_qobuz_signed_json_request(path: str, params: dict) -> dict:
    """
    Execute a signed GET request against the Qobuz API.
    Auto-refreshes credentials on 400/401.  Returns parsed JSON dict.
    """
    def _call(force_refresh: bool) -> requests.Response:
        creds = _get_qobuz_api_credentials(force_refresh=force_refresh)
        signed = _qobuz_signed_params(path, params, creds)
        return requests.get(
            f"{_QOBUZ_API_BASE}/{path}",
            params=signed,
            headers={
                "User-Agent": _QOBUZ_UA,
                "Accept":     "application/json",
                "X-App-Id":   creds["app_id"],
            },
            timeout=15,
        )

    resp = _call(False)
    if resp.status_code in (400, 401):
        resp.close()
        resp = _call(True)
    resp.raise_for_status()
    return resp.json()


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
# Spotify access token (in-memory + disk cache)
# ---------------------------------------------------------------------------
_token_cache: dict = {}


def _spotify_token_cache_path() -> Path:
    cache_dir = Path(tempfile.gettempdir()) / "tele2rub"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "spotify-anon-token.json"


def _load_spotify_token() -> dict:
    """Load cached Spotify token from disk. Returns {} on failure."""
    try:
        data = json.loads(_spotify_token_cache_path().read_text())
        if data.get("token") and data.get("expires_at"):
            return data
    except Exception:
        pass
    return {}


def _save_spotify_token(token: str, expires_at: float) -> None:
    """Persist Spotify token to disk cache."""
    try:
        _spotify_token_cache_path().write_text(
            json.dumps({"token": token, "expires_at": expires_at})
        )
    except Exception as exc:
        log.debug("could not save spotify token cache: %s", exc)

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
    """Get an anonymous Spotify access token (matches SpotiFLAC requestSpotifyAnonymousAccessToken)."""
    totp_code = _totp(_SPOTIFY_TOTP_SECRET)
    params: dict = {
        "reason":      "init",
        "productType": "web-player",
        "totp":        totp_code,
        "totpVer":     str(_SPOTIFY_TOTP_VERSION),
        "totpServer":  totp_code,
    }
    resp = requests.get(
        "https://open.spotify.com/api/token",
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json;charset=UTF-8",
        },
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
    """Return a valid Spotify Bearer token (in-memory cache → disk cache → network)."""
    now = time.time()

    # 1. In-memory cache
    if _token_cache.get("expires_at", 0) > now + 30:
        return _token_cache["token"]

    # 2. Disk cache
    if not _token_cache:
        disk = _load_spotify_token()
        if disk.get("expires_at", 0) > now + 30:
            _token_cache.update(disk)
            log.debug("spotify token loaded from disk cache")
            return _token_cache["token"]

    # 3. Anonymous web-player token
    try:
        token, expires = _fetch_anon_token()
        _token_cache.update({"token": token, "expires_at": expires})
        _save_spotify_token(token, expires)
        log.debug("spotify anon token OK (expires %s)", time.ctime(expires))
        return token
    except Exception as exc:
        log.warning("anon token failed: %s", exc)

    # 4. Client-credentials fallback
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
        "Set SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET as a fallback."
    )


# ---------------------------------------------------------------------------
# Metadata fetching
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    return {**_HEADERS_BASE, "Authorization": f"Bearer {get_token()}"}


def _spclient_file_id_to_hex(fid: str) -> str:
    """
    Convert a Spotify file_id to the hex form expected by i.scdn.co.
    Handles both hex-encoded and base64-encoded variants.
    Ref: SpotiFLAC backend/isrc_finder.go
    """
    fid = fid.strip()
    if not fid:
        return ""
    if re.fullmatch(r'[0-9a-fA-F]{32,40}', fid):
        return fid.lower()
    try:
        decoded = base64.b64decode(fid + "==")
        return decoded.hex()
    except Exception:
        return fid.lower()


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
            hex_fid = _spclient_file_id_to_hex(fid)
            cover_url = f"https://i.scdn.co/image/{hex_fid}" if hex_fid else ""

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


# ---------------------------------------------------------------------------
# Spotify internal GraphQL API (persisted queries)
# ---------------------------------------------------------------------------

_SPOTIFY_GRAPHQL_ENDPOINT = "https://api-partner.spotify.com/pathfinder/v1/query"

# Persisted query sha256 hashes for Spotify's internal GraphQL operations
_GRAPHQL_HASH_GET_TRACK       = "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294"
_GRAPHQL_HASH_GET_ALBUM       = "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10"
_GRAPHQL_HASH_FETCH_PLAYLIST  = "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"


def _spotify_graphql_query(payload: dict) -> dict:
    """
    Send a persisted GraphQL query to Spotify's internal partner API.
    Uses a GET request with JSON-encoded variables and extensions as query params.
    """
    params = {
        "operationName": payload["operationName"],
        "variables":     json.dumps(payload.get("variables", {}), separators=(",", ":")),
        "extensions":    json.dumps(payload.get("extensions", {}), separators=(",", ":")),
    }
    resp = requests.get(
        _SPOTIFY_GRAPHQL_ENDPOINT,
        params=params,
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_track_graphql(track_id: str) -> dict:
    """Fetch track metadata via Spotify's internal GraphQL getTrack query."""
    return _spotify_graphql_query({
        "variables":     {"uri": f"spotify:track:{track_id}"},
        "operationName": "getTrack",
        "extensions": {
            "persistedQuery": {
                "version":    1,
                "sha256Hash": _GRAPHQL_HASH_GET_TRACK,
            }
        },
    })


def _parse_graphql_track(data: dict) -> dict:
    """Parse a Spotify GraphQL getTrack response into the standard info dict."""
    track = ((data.get("data") or {}).get("trackUnion") or {})

    # Artists
    artist_items = ((track.get("artists") or {}).get("items") or [])
    artists = [
        (item.get("profile") or {}).get("name", "")
        for item in artist_items
        if (item.get("profile") or {}).get("name")
    ]

    # Album
    album = (track.get("albumOfTrack") or {})

    # Release date — isoString is like "2024-01-15T00:00:00Z"
    release_date = ""
    date_obj = (album.get("date") or {})
    iso = date_obj.get("isoString", "")
    if iso:
        release_date = iso[:10]  # YYYY-MM-DD
    elif date_obj.get("year"):
        release_date = str(date_obj["year"])

    # Cover art — sources ordered largest first
    cover_url = ""
    sources = ((album.get("coverArt") or {}).get("sources") or [])
    if sources:
        cover_url = sources[0].get("url", "")

    # ISRC
    isrc = ((track.get("externalIds") or {}).get("isrc") or None)

    return {
        "title":        track.get("name", ""),
        "artists":      artists,
        "album":        (album.get("name") or ""),
        "release_date": release_date,
        "cover_url":    cover_url,
        "track_number": track.get("trackNumber", 1),
        "disc_number":  track.get("discNumber", 1),
        "isrc":         isrc,
    }


def _fetch_album_graphql_page(album_id: str, offset: int, limit: int) -> dict:
    """Fetch a page of album data via Spotify's internal GraphQL getAlbum query."""
    return _spotify_graphql_query({
        "variables": {
            "uri":    f"spotify:album:{album_id}",
            "locale": "",
            "offset": offset,
            "limit":  limit,
        },
        "operationName": "getAlbum",
        "extensions": {
            "persistedQuery": {
                "version":    1,
                "sha256Hash": _GRAPHQL_HASH_GET_ALBUM,
            }
        },
    })


def _fetch_playlist_graphql_page(playlist_id: str, offset: int, limit: int) -> dict:
    """Fetch a page of playlist data via Spotify's internal GraphQL fetchPlaylist query."""
    return _spotify_graphql_query({
        "variables": {
            "uri":                       f"spotify:playlist:{playlist_id}",
            "offset":                    offset,
            "limit":                     limit,
            "enableWatchFeedEntrypoint": False,
        },
        "operationName": "fetchPlaylist",
        "extensions": {
            "persistedQuery": {
                "version":    1,
                "sha256Hash": _GRAPHQL_HASH_FETCH_PLAYLIST,
            }
        },
    })


# ---------------------------------------------------------------------------
# Spotify v2 client (session-based auth: accessToken + clientToken)
# ---------------------------------------------------------------------------

class SpotifyClient:
    """
    Session-based Spotify client that uses the web-player auth flow:
    1. Scrape clientVersion from open.spotify.com HTML.
    2. Fetch accessToken + clientId via /api/token (TOTP-authenticated).
    3. Fetch clientToken from clienttoken.spotify.com.
    4. POST queries to api-partner.spotify.com/pathfinder/v2/query.
    """

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self._UA})
        self._access_token: str = ""
        self._client_token: str = ""
        self._client_id: str = ""
        self._device_id: str = ""
        self._client_version: str = ""

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _get_session_info(self) -> None:
        """Scrape clientVersion from the open.spotify.com HTML."""
        resp = self._session.get("https://open.spotify.com", timeout=30)
        resp.raise_for_status()
        m = re.search(
            r'<script id="appServerConfig" type="text/plain">([^<]+)</script>',
            resp.text,
        )
        if m:
            try:
                cfg = json.loads(base64.b64decode(m.group(1)).decode())
                self._client_version = cfg.get("clientVersion", "")
            except Exception:
                pass
        if not self._client_version:
            self._client_version = _SPOTIFY_CLIENT_VERSION_FALLBACK
            log.debug("spotify: using fallback client version %s", self._client_version)
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_access_token(self) -> None:
        """Fetch accessToken + clientId via TOTP-authenticated /api/token."""
        totp_code = _totp(_SPOTIFY_TOTP_SECRET)
        resp = self._session.get(
            "https://open.spotify.com/api/token",
            params={
                "reason":      "init",
                "productType": "web-player",
                "totp":        totp_code,
                "totpVer":     str(_SPOTIFY_TOTP_VERSION),
                "totpServer":  totp_code,
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"spotify access token request failed: HTTP {resp.status_code}"
            )
        data = resp.json()
        self._access_token = data.get("accessToken", "")
        self._client_id = data.get("clientId", "")
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_client_token(self) -> None:
        """Fetch clientToken from clienttoken.spotify.com."""
        if not self._client_id or not self._device_id or not self._client_version:
            self._get_session_info()
            self._get_access_token()

        payload = {
            "client_data": {
                "client_version": self._client_version,
                "client_id":      self._client_id,
                "js_sdk_data": {
                    "device_brand": "unknown",
                    "device_model": "unknown",
                    "os":           "windows",
                    "os_version":   "NT 10.0",
                    "device_id":    self._device_id,
                    "device_type":  "computer",
                },
            }
        }
        resp = self._session.post(
            "https://clienttoken.spotify.com/v1/clienttoken",
            json=payload,
            headers={
                "Authority":    "clienttoken.spotify.com",
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"spotify client token request failed: HTTP {resp.status_code}"
            )
        data = resp.json()
        if data.get("response_type") != "RESPONSE_GRANTED_TOKEN_RESPONSE":
            raise RuntimeError(
                f"invalid client token response type: {data.get('response_type')!r}"
            )
        self._client_token = (data.get("granted_token") or {}).get("token", "")

    def initialize(self) -> None:
        """Run the full auth flow: session → access token → client token."""
        self._get_session_info()
        self._get_access_token()
        self._get_client_token()

    def query(self, payload: dict) -> dict:
        """
        POST a query to api-partner.spotify.com/pathfinder/v2/query.
        Auto-initializes auth on the first call.
        """
        if not self._access_token or not self._client_token:
            self.initialize()

        resp = self._session.post(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            json=payload,
            headers={
                "Authorization":      f"Bearer {self._access_token}",
                "Client-Token":       self._client_token,
                "Spotify-App-Version": self._client_version,
                "Content-Type":       "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            snippet = resp.text[:200]
            raise RuntimeError(
                f"spotify API query failed: HTTP {resp.status_code} | {snippet}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Filter helper utilities (shared by filter_track / filter_album / filter_playlist)
# ---------------------------------------------------------------------------

def _sp_str(m: dict, key: str) -> str:
    v = m.get(key)
    return v if isinstance(v, str) else ""


def _sp_map(m: dict, key: str) -> dict:
    v = m.get(key)
    return v if isinstance(v, dict) else {}


def _sp_list(m: dict, key: str) -> list:
    v = m.get(key)
    return v if isinstance(v, list) else []


def _sp_float(m: dict, key: str) -> float:
    v = m.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0


def _sp_extract_artists(artists_data: dict) -> list[dict]:
    artists = []
    for item in _sp_list(artists_data, "items"):
        if not isinstance(item, dict):
            continue
        name = _sp_str(_sp_map(item, "profile"), "name")
        if name:
            artists.append({"name": name})
    return artists


def _sp_extract_cover(cover_data: dict) -> dict | None:
    """Parse a Spotify cover-art block into a {small, medium, large} URL dict."""
    if not cover_data:
        return None

    sources: list = []
    if isinstance(cover_data.get("sources"), list):
        sources = cover_data["sources"]
    else:
        try:
            sources = cover_data["squareCoverImage"]["image"]["data"]["sources"]
        except (KeyError, TypeError):
            pass

    if not sources:
        return None

    filtered = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = _sp_str(s, "url")
        if not url:
            continue
        width  = _sp_float(s, "width")  or _sp_float(s, "maxWidth")
        height = _sp_float(s, "height") or _sp_float(s, "maxHeight")
        if (width > 64 and height > 64) or (width == 0 and height == 0):
            filtered.append({"url": url, "width": width, "height": height})

    if not filtered:
        return None

    filtered.sort(key=lambda x: x["width"])

    small_url = medium_url = image_id = fallback_url = ""
    for src in filtered:
        url   = src["url"]
        width = src["width"]
        if width == 300:
            small_url = url
        elif width == 640:
            medium_url = url
        elif width == 0:
            fallback_url = url

        if not image_id and url:
            for marker in ("ab67616d0000b273", "ab67616d00001e02"):
                if marker in url:
                    image_id = url.split(marker)[-1]
                    break
            else:
                if "/image/" in url:
                    img_part = url.split("/image/")[-1].split("?")[0]
                    if len(img_part) > 20:
                        for prefix in (
                            "ab67616d0000b273",
                            "ab67616d00001e02",
                            "ab67616d00004851",
                        ):
                            if prefix in img_part:
                                image_id = img_part.split(prefix)[-1]
                                break

    large_url = (
        f"https://i.scdn.co/image/ab67616d000082c1{image_id}" if image_id else ""
    )

    result: dict = {}
    if small_url:
        result["small"] = small_url
    if medium_url:
        result["medium"] = medium_url
    if large_url:
        result["large"] = large_url
    if not result and fallback_url:
        result = {"small": fallback_url, "medium": fallback_url, "large": fallback_url}

    return result or None


def _sp_extract_duration(ms: float) -> str:
    total_s = int(ms) // 1000
    return f"{total_s // 60}:{total_s % 60:02d}"


# ---------------------------------------------------------------------------
# Rich filter functions for v2 GraphQL responses
# ---------------------------------------------------------------------------

def filter_track(
    data: dict,
    separator: str = ", ",
    album_fetch_data: dict | None = None,
) -> dict:
    """
    Parse a Spotify v2 getTrack GraphQL response into a structured dict.

    Args:
        data: Raw JSON response from the v2 query endpoint.
        separator: String used to join multiple artist names.
        album_fetch_data: Optional raw response from a getAlbum query for the
            same album — used to fill in label and total-disc information.

    Returns a dict with keys: id, name, artists, album, duration, track, disc,
    discs, copyright, plays, cover, is_explicit.
    """
    track_data = _sp_map(_sp_map(data, "data"), "trackUnion")
    if not track_data:
        return {}

    # Artists — try several response shapes
    artists: list[dict] = _sp_extract_artists(_sp_map(track_data, "artists"))
    if not artists:
        for key in ("firstArtist", "otherArtists"):
            for item in _sp_list(_sp_map(track_data, key), "items"):
                if not isinstance(item, dict):
                    continue
                name = _sp_str(_sp_map(item, "profile"), "name")
                if name:
                    artists.append({"name": name})
    if not artists:
        artists = _sp_extract_artists(
            _sp_map(_sp_map(track_data, "albumOfTrack"), "artists")
        )

    artists_str = separator.join(a["name"] for a in artists)

    # Album data
    album_data = _sp_map(track_data, "albumOfTrack")
    album_info: dict | None = None
    copyright_texts: list[str] = []
    disc_info_total: int | None = None

    if album_data:
        # Copyright notices (C-type only)
        for item in _sp_list(_sp_map(album_data, "copyright"), "items"):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "P":
                t = _sp_str(item, "text")
                if t:
                    copyright_texts.append(t)

        # Total discs from album track list
        disc_numbers: set[int] = set()
        for item in _sp_list(_sp_map(album_data, "tracks"), "items"):
            if not isinstance(item, dict):
                continue
            d = int(_sp_float(_sp_map(item, "track"), "discNumber")) or 1
            disc_numbers.add(d)
        if disc_numbers:
            disc_info_total = max(disc_numbers)

        # Release date
        date_info = _sp_map(album_data, "date")
        iso = _sp_str(date_info, "isoString")
        release_year: int | None
        if iso:
            release_date = iso[:10]
            release_year = int(iso[:4]) if len(iso) >= 4 else None
        else:
            y  = _sp_str(date_info, "year")
            mo = _sp_str(date_info, "month")
            dy = _sp_str(date_info, "day")
            if y:
                release_year = int(y)
                release_date = (
                    f"{y}-{int(mo):02d}-{int(dy):02d}" if mo and dy else y
                )
            else:
                release_date = ""
                release_year = None

        tracks_data  = _sp_map(album_data, "tracks")
        tracks_count = int(_sp_float(tracks_data, "totalCount"))

        album_uri = _sp_str(album_data, "uri")
        album_id  = _sp_str(album_data, "id") or (
            album_uri.split(":")[-1] if ":" in album_uri else ""
        )

        album_artists_str = ""
        album_label       = ""
        if album_fetch_data:
            album_union = _sp_map(_sp_map(album_fetch_data, "data"), "albumUnion")
            if album_union:
                al = _sp_extract_artists(_sp_map(album_union, "artists"))
                album_artists_str = separator.join(a["name"] for a in al)
                album_label       = _sp_str(album_union, "label")
        if not album_artists_str:
            al = _sp_extract_artists(_sp_map(album_data, "artists"))
            album_artists_str = separator.join(a["name"] for a in al)

        album_info = {
            "id":       album_id,
            "name":     _sp_str(album_data, "name"),
            "released": release_date,
            "year":     release_year,
            "tracks":   tracks_count,
        }
        if album_artists_str:
            album_info["artists"] = album_artists_str
        if album_label:
            album_info["label"] = album_label

    # Cover art
    cover = _sp_extract_cover(_sp_map(track_data, "visualIdentity"))
    if cover is None and album_data:
        cover = _sp_extract_cover(_sp_map(album_data, "coverArt"))

    # Duration
    duration_ms  = _sp_float(_sp_map(track_data, "duration"), "totalMilliseconds")
    duration_str = _sp_extract_duration(duration_ms)

    # Disc number resolution
    disc_number = int(_sp_float(track_data, "discNumber")) or 1
    max_disc_from_album   = 0
    total_discs_from_album = 0
    if album_fetch_data:
        album_union = _sp_map(_sp_map(album_fetch_data, "data"), "albumUnion")
        if album_union:
            total_discs_from_album = int(
                _sp_float(_sp_map(album_union, "discs"), "totalCount")
            )
            current_id = _sp_str(track_data, "id")
            for item in _sp_list(_sp_map(album_union, "tracks"), "items"):
                if not isinstance(item, dict):
                    continue
                ti    = _sp_map(item, "track")
                d_num = int(_sp_float(ti, "discNumber"))
                if d_num > max_disc_from_album:
                    max_disc_from_album = d_num
                track_uri = _sp_str(ti, "uri")
                if current_id in track_uri or _sp_str(ti, "id") == current_id:
                    if d_num > 0:
                        disc_number = d_num

    if total_discs_from_album > 0:
        total_discs = total_discs_from_album
    elif max_disc_from_album > 0:
        total_discs = max_disc_from_album
    elif disc_info_total is not None:
        total_discs = disc_info_total
    else:
        total_discs = 1

    content_rating = _sp_map(track_data, "contentRating")
    is_explicit    = _sp_str(content_rating, "label") == "EXPLICIT"

    return {
        "id":          _sp_str(track_data, "id"),
        "name":        _sp_str(track_data, "name"),
        "artists":     artists_str,
        "album":       album_info,
        "duration":    duration_str,
        "track":       int(_sp_float(track_data, "trackNumber")),
        "disc":        disc_number,
        "discs":       total_discs,
        "copyright":   ", ".join(copyright_texts),
        "plays":       _sp_str(track_data, "playcount"),
        "cover":       cover,
        "is_explicit": is_explicit,
    }


def filter_album(data: dict, separator: str = ", ") -> dict:
    """
    Parse a Spotify v2 getAlbum GraphQL response into a structured dict.

    Returns a dict with keys: id, name, artists, cover, releaseDate, count,
    tracks (list), discs, label.
    """
    album_data = _sp_map(_sp_map(data, "data"), "albumUnion")
    if not album_data:
        return {}

    artists     = _sp_extract_artists(_sp_map(album_data, "artists"))
    artists_str = separator.join(a["name"] for a in artists)

    cover_obj = _sp_extract_cover(_sp_map(album_data, "coverArt"))
    cover: str | None = None
    if cover_obj:
        cover = cover_obj.get("small") or cover_obj.get("medium") or cover_obj.get("large")

    tracks: list[dict] = []
    for item in _sp_list(_sp_map(album_data, "tracksV2"), "items"):
        if not isinstance(item, dict):
            continue
        track = _sp_map(item, "track")
        if not track:
            continue

        artists_data     = _sp_map(track, "artists")
        track_artists    = _sp_extract_artists(artists_data)
        track_artists_str = separator.join(a["name"] for a in track_artists)

        artist_ids: list[str] = []
        for ai in _sp_list(artists_data, "items"):
            if not isinstance(ai, dict):
                continue
            uri = _sp_str(ai, "uri")
            if ":" in uri:
                artist_ids.append(uri.split(":")[-1])

        track_uri = _sp_str(track, "uri")
        track_id  = track_uri.split(":")[-1] if ":" in track_uri else ""

        duration_ms = _sp_float(_sp_map(track, "duration"), "totalMilliseconds")
        disc        = int(_sp_float(track, "discNumber")) or 1

        content_rating = _sp_map(track, "contentRating")
        is_explicit    = _sp_str(content_rating, "label") == "EXPLICIT"

        tracks.append({
            "id":          track_id,
            "name":        _sp_str(track, "name"),
            "artists":     track_artists_str,
            "artistIds":   artist_ids,
            "duration":    _sp_extract_duration(duration_ms),
            "plays":       _sp_str(track, "playcount"),
            "is_explicit": is_explicit,
            "disc_number": disc,
        })

    date_info    = _sp_map(album_data, "date")
    iso          = _sp_str(date_info, "isoString")
    release_date = iso[:10] if iso else ""

    album_uri = _sp_str(album_data, "uri")
    album_id  = album_uri.split(":")[-1] if ":" in album_uri else ""

    discs_data  = _sp_map(album_data, "discs")
    total_discs = int(_sp_float(discs_data, "totalCount")) or 1

    return {
        "id":          album_id,
        "name":        _sp_str(album_data, "name"),
        "artists":     artists_str,
        "cover":       cover,
        "releaseDate": release_date,
        "count":       len(tracks),
        "tracks":      tracks,
        "discs":       {"totalCount": total_discs},
        "label":       _sp_str(album_data, "label"),
    }


def filter_playlist(data: dict, separator: str = ", ") -> dict:
    """
    Parse a Spotify v2 fetchPlaylist GraphQL response into a structured dict.

    Returns a dict with keys: id, name, description, owner, cover, followers,
    count, tracks (list).
    """
    playlist_data = _sp_map(_sp_map(data, "data"), "playlistV2")
    if not playlist_data:
        return {}

    owner_data = _sp_map(_sp_map(playlist_data, "ownerV2"), "data")
    owner_info: dict | None = None
    if owner_data:
        avatar_url: str | None = None
        avatar_sources = _sp_list(_sp_map(owner_data, "avatar"), "sources")
        if avatar_sources and isinstance(avatar_sources[0], dict):
            avatar_url = _sp_str(avatar_sources[0], "url") or None
        owner_info = {
            "name":   _sp_str(owner_data, "name"),
            "avatar": avatar_url,
        }

    images_data = _sp_map(playlist_data, "images") or _sp_map(playlist_data, "imagesV2")
    cover: str | None = None
    image_items = _sp_list(images_data, "items")
    if image_items and isinstance(image_items[0], dict):
        first_sources = _sp_list(image_items[0], "sources")
        if first_sources and isinstance(first_sources[0], dict):
            cover = _sp_str(first_sources[0], "url") or None
    if cover is None:
        img_sources = _sp_list(images_data, "sources")
        if img_sources and isinstance(img_sources[0], dict):
            cover = _sp_str(img_sources[0], "url") or None

    tracks: list[dict] = []
    for item in _sp_list(_sp_map(playlist_data, "content"), "items"):
        if not isinstance(item, dict):
            continue
        track_data = _sp_map(_sp_map(item, "itemV2"), "data")
        if not track_data:
            continue

        track_name = _sp_str(track_data, "name")
        if not track_name:
            continue

        rank = status = None
        for attr in _sp_list(item, "attributes"):
            if not isinstance(attr, dict):
                continue
            k = _sp_str(attr, "key")
            if k == "rank":
                rank   = _sp_str(attr, "value")
            elif k == "status":
                status = _sp_str(attr, "value")

        artists_data      = _sp_map(track_data, "artists")
        track_artists     = _sp_extract_artists(artists_data)
        track_artists_str = separator.join(a["name"] for a in track_artists)

        artist_ids: list[str] = []
        for ai in _sp_list(artists_data, "items"):
            if not isinstance(ai, dict):
                continue
            uri = _sp_str(ai, "uri")
            if ":" in uri:
                artist_ids.append(uri.split(":")[-1])

        track_uri = _sp_str(track_data, "uri")
        track_id  = _sp_str(track_data, "id") or (
            track_uri.split(":")[-1] if ":" in track_uri else ""
        )

        album_data       = _sp_map(track_data, "albumOfTrack")
        album_name       = album_id = album_artists_str = ""
        track_cover: str | None = None
        if album_data:
            album_name  = _sp_str(album_data, "name")
            album_uri   = _sp_str(album_data, "uri")
            album_id    = album_uri.split(":")[-1] if ":" in album_uri else ""
            cover_obj   = _sp_extract_cover(_sp_map(album_data, "coverArt"))
            if cover_obj:
                track_cover = (
                    cover_obj.get("small") or
                    cover_obj.get("medium") or
                    cover_obj.get("large")
                )
            al = _sp_extract_artists(_sp_map(album_data, "artists"))
            album_artists_str = separator.join(a["name"] for a in al)

        duration_ms    = _sp_float(_sp_map(track_data, "trackDuration"), "totalMilliseconds")
        content_rating = _sp_map(track_data, "contentRating")
        is_explicit    = _sp_str(content_rating, "label") == "EXPLICIT"

        tracks.append({
            "id":          track_id,
            "cover":       track_cover,
            "title":       track_name,
            "artist":      track_artists_str,
            "artistIds":   artist_ids,
            "plays":       rank,
            "status":      status,
            "album":       album_name,
            "albumArtist": album_artists_str,
            "albumId":     album_id,
            "duration":    _sp_extract_duration(duration_ms),
            "is_explicit": is_explicit,
            "disc_number": int(_sp_float(track_data, "discNumber")),
        })

    followers_data = playlist_data.get("followers")
    followers: float | None = None
    if isinstance(followers_data, dict):
        v = _sp_float(followers_data, "totalCount")
        followers = v if v else None

    playlist_uri = _sp_str(playlist_data, "uri")
    playlist_id  = playlist_uri.split(":")[-1] if ":" in playlist_uri else ""

    return {
        "id":          playlist_id,
        "name":        _sp_str(playlist_data, "name"),
        "description": html.unescape(_sp_str(playlist_data, "description")),
        "owner":       owner_info,
        "cover":       cover,
        "followers":   followers,
        "count":       len(tracks),
        "tracks":      tracks,
    }



# ---------------------------------------------------------------------------
# ISRC disk cache (matches SpotiFLAC GetCachedISRC / PutCachedISRC)
# ---------------------------------------------------------------------------

_ISRC_CACHE_FILE = "spotify-isrc-cache.json"
_isrc_cache_lock = threading.Lock()


def _isrc_cache_path() -> Path:
    cache_dir = Path(tempfile.gettempdir()) / "tele2rub"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _ISRC_CACHE_FILE


def _get_cached_isrc(track_id: str) -> str | None:
    """Return cached ISRC for a Spotify track ID, or None."""
    try:
        with _isrc_cache_lock:
            data = json.loads(_isrc_cache_path().read_text())
        return data.get(track_id) or None
    except Exception:
        return None


def _put_cached_isrc(track_id: str, isrc: str) -> None:
    """Persist a track_id → ISRC mapping to the disk cache."""
    try:
        with _isrc_cache_lock:
            path = _isrc_cache_path()
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = {}
            data[track_id] = isrc
            path.write_text(json.dumps(data))
    except Exception as exc:
        log.debug("could not save isrc cache: %s", exc)


# ---------------------------------------------------------------------------
# (existing code continues below)
# ---------------------------------------------------------------------------

_ISRC_RE = re.compile(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}')


def _isrc_soundplate(track_id: str) -> str | None:
    """Last-resort ISRC lookup via Soundplate (matches SpotiFLAC lookupSpotifyISRCViaSoundplate)."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://phpstack-822472-6184058.cloudwaysapps.com/?",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = requests.get(
            "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php",
            params={"q": f"https://open.spotify.com/track/{track_id}"},
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            body = resp.text
            # try JSON field first
            try:
                data = resp.json()
                isrc = data.get("isrc") or (data.get("data") or {}).get("isrc") or ""
                if isrc:
                    m = _ISRC_RE.search(isrc.upper())
                    if m:
                        return m.group(0)
            except Exception:
                pass
            # fallback: regex search over entire body
            m = _ISRC_RE.search(body.upper())
            if m:
                return m.group(0)
    except Exception as exc:
        log.warning("soundplate fallback: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Deezer resolution
# ---------------------------------------------------------------------------

# Spotify CDN size hash constants (Ref: SpotiFLAC backend/cover.go)
_SPOTIFY_COVER_300 = "ab67616d00001e02"
_SPOTIFY_COVER_640 = "ab67616d0000b273"
_SPOTIFY_COVER_MAX = "ab67616d000082c1"


def _upgrade_spotify_cover_url(url: str) -> str:
    """
    Upgrade a Spotify cover URL to maximum resolution (300px → 640px → max).
    Ref: SpotiFLAC backend/cover.go getMaxResolutionURL()
    """
    if not url:
        return url
    if _SPOTIFY_COVER_300 in url:
        url = url.replace(_SPOTIFY_COVER_300, _SPOTIFY_COVER_MAX)
    elif _SPOTIFY_COVER_640 in url:
        url = url.replace(_SPOTIFY_COVER_640, _SPOTIFY_COVER_MAX)
    return url


def _deezer_url_from_isrc(isrc: str) -> str | None:
    """
    Resolve a Deezer track URL from an ISRC using the free Deezer public API.
    No credentials required.  Ref: SpotiFLAC songlink.go lookupDeezerTrackURLByISRC()
    """
    try:
        url = f"https://api.deezer.com/track/isrc:{isrc.upper().strip()}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if resp.ok:
            data = resp.json()
            if "error" in data:
                return None
            link = data.get("link", "")
            track_id = data.get("id", 0)
            if link:
                return link
            if track_id:
                return f"https://www.deezer.com/track/{track_id}"
    except Exception as exc:
        log.debug("deezer isrc->url: %s", exc)
    return None


def _deezer_isrc_from_url(deezer_url: str) -> str | None:
    """
    Fetch the ISRC from a Deezer track using the public Deezer API.
    No credentials required. Ref: SpotiFLAC songlink.go getDeezerISRC()
    """
    try:
        m = re.search(r'/track/(\d+)', deezer_url)
        if not m:
            return None
        track_id = m.group(1)
        resp = requests.get(
            f"https://api.deezer.com/track/{track_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            isrc = data.get("isrc", "").strip().upper()
            if isrc:
                return isrc
    except Exception as exc:
        log.debug("deezer url->isrc: %s", exc)
    return None


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
    """Return the first Qobuz track dict matching the ISRC, or None.
    Uses auto-scraped credentials — no QOBUZ_APP_ID env var required.
    If ISRC starts with 'qobuz_', treats the remainder as a direct numeric Qobuz track ID.
    Ref: SpotiFLAC backend/qobuz.go searchByISRC()
    """
    if isrc.startswith("qobuz_"):
        track_id = isrc[len("qobuz_"):]
        return _get_qobuz_track(track_id)
    try:
        data = _do_qobuz_signed_json_request(
            "track/search", {"query": isrc, "limit": "5"}
        )
        tracks = (data.get("tracks") or {}).get("items") or []
        for t in tracks:
            if (t.get("isrc") or "").upper() == isrc.upper():
                return t
    except Exception as exc:
        log.warning("qobuz ISRC lookup: %s", exc)
    return None


def _get_qobuz_track(track_id: str) -> dict | None:
    """Fetch a Qobuz track by its numeric ID using auto-scraped credentials."""
    try:
        data = _do_qobuz_signed_json_request(
            "track/get", {"track_id": str(track_id)}
        )
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
    Returns a dict of {deezer_url, qobuz_url, tidal_url, amazon_url, spotify_url}
    (keys absent when the platform was not found).  No API key required.
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
        # Handle both "qobuz" and "qobuzStore" keys
        qobuz_link = links.get("qobuz") or links.get("qobuzStore")
        if qobuz_link:
            result["qobuz_url"] = qobuz_link.get("url", "")
        # Handle both "amazonMusic" and "amazon" keys
        amazon_link = links.get("amazonMusic") or links.get("amazon")
        if amazon_link:
            result["amazon_url"] = amazon_link.get("url", "")
        # Extract Spotify URL — useful when input was Tidal/Qobuz/Amazon
        if "spotify" in links:
            result["spotify_url"] = links["spotify"]["url"]
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
    structured-data (application/ld+json sameAs blocks) or href attributes.
    No auth required.  Returns a dict with any of: deezer_url, tidal_url, amazon_url.
    """
    try:
        url = f"https://songstats.com/track/{isrc.upper()}"
        resp = requests.get(
            url,
            params={"ref": "ISRCFinder"},
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "text/html",
            },
            timeout=15,
        )
        if not resp.ok:
            return {}
        html_text = resp.text
        result: dict = {}

        # ── JSON-LD structured data ────────────────────────────────────────
        for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.S):
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

        # ── Fallback: href regex scan (handles current Songstats HTML) ────
        if "tidal_url" not in result:
            m = re.search(r'href="(https://tidal\.com/browse/track/\d+)"', html_text)
            if m:
                result["tidal_url"] = m.group(1)
        if "amazon_url" not in result:
            m = re.search(r'href="(https://music\.amazon\.com/[^"]+)"', html_text)
            if m:
                result["amazon_url"] = m.group(1)
        if "deezer_url" not in result:
            m = re.search(r'href="(https://www\.deezer\.com/track/\d+)"', html_text)
            if m:
                result["deezer_url"] = m.group(1)

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


def _upgrade_tidal_cover_url(url: str) -> str:
    """Upgrade a Tidal cover URL to 1280×1280 for best quality."""
    if not url:
        return url
    return re.sub(r'/(\d+)x(\d+)\.jpg$', '/1280x1280.jpg', url)


def _parse_tidal_track(data: dict) -> dict:
    """Convert a Tidal track API dict to the standard info dict."""
    album = data.get("album") or {}
    # Cover art: https://resources.tidal.com/images/<uuid-with-dashes>/640x640.jpg
    cover_id = album.get("cover", "").replace("-", "/")
    cover_url = _upgrade_tidal_cover_url(
        f"https://resources.tidal.com/images/{cover_id}/640x640.jpg" if cover_id else ""
    )
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
# MusicBrainz genre enrichment (matches SpotiFLAC backend/musicbrainz.go)
# ---------------------------------------------------------------------------

_mb_lock = threading.Lock()
_mb_last_call = 0.0  # enforce 1.1s minimum interval


def _musicbrainz_genre(isrc: str, max_genres: int = 3) -> str:
    """
    Look up genre tags from MusicBrainz for the given ISRC.
    Returns a comma-separated genre string, or "" on failure.
    Ref: SpotiFLAC backend/musicbrainz.go FetchMusicBrainzMetadata()
    """
    global _mb_last_call
    if not isrc:
        return ""
    try:
        with _mb_lock:
            # Respect MusicBrainz rate limit (1 req/sec)
            wait = 1.1 - (time.time() - _mb_last_call)
            if wait > 0:
                time.sleep(wait)
            _mb_last_call = time.time()

        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={
                "query": f"isrc:{isrc}",
                "fmt":   "json",
                "inc":   "tags",
                "limit": "1",
            },
            headers={"User-Agent": "Tele2Rub/1.0 (https://github.com/xshayank/Tele2Rub)"},
            timeout=10,
        )
        if not resp.ok:
            return ""
        recordings = resp.json().get("recordings") or []
        if not recordings:
            return ""
        tags = recordings[0].get("tags") or []
        if not tags:
            return ""
        tags.sort(key=lambda t: t.get("count", 0), reverse=True)
        genres = [t["name"].title() for t in tags[:max_genres] if t.get("name")]
        return ", ".join(genres)
    except Exception as exc:
        log.debug("musicbrainz genre lookup: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Tidal Alt proxy (no token required — matches SpotiFLAC backend/tidal_alt.go)
# Gap 5: endpoint rotation via TIDAL_ALT_BASES list (SpotiFLAC backend/tidal_api_list.go)
# ---------------------------------------------------------------------------

_TIDAL_ALT_API_BASE = TIDAL_ALT_BASES[0]


def _parse_tidal_alt_response(resp: "requests.Response") -> "str | dict | None":
    """
    Parse a Tidal Alt proxy response.

    Returns:
      - A plain URL string for direct single-file downloads.
      - A dict with keys {"type": "manifest", "urls": [...], "codecs": "...", "mimeType": "..."}
        when the proxy returns a V2 BTS manifest (Gap 2 — SpotiFLAC backend/tidal.go
        DownloadFromManifest / GetDownloadURL).
      - None if no usable response.
    """
    # HTTP redirect → direct URL
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if loc.startswith("http"):
            return loc

    if not resp.ok:
        return None

    ct = resp.headers.get("content-type", "")

    # Plain-text URL
    if "text/plain" in ct:
        txt = resp.text.strip()
        if txt.startswith("http"):
            return txt

    # JSON
    try:
        data = resp.json()
    except Exception:
        txt = resp.text.strip()
        if txt.startswith("http"):
            return txt
        return None

    # V2 manifest response: {"data": {"manifest": "<base64>"}}
    # Ref: SpotiFLAC backend/tidal.go GetDownloadURL / DownloadFromManifest
    manifest_b64 = (data.get("data") or {}).get("manifest")
    if manifest_b64:
        try:
            # Pad to a multiple of 4 chars as required by base64
            padded = manifest_b64 + "=" * (-len(manifest_b64) % 4)
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

    # Standard JSON: link / url field
    url = (data.get("link") or data.get("url") or "").strip()
    if url.startswith("http"):
        return url

    # Body might literally be a URL
    txt = resp.text.strip()
    if txt.startswith("http"):
        return txt

    return None


def _get_tidal_alt_url(spotify_track_id: str) -> "str | dict | None":
    """
    Fetch a Tidal download URL/manifest via the no-auth SpotiFLAC proxy.
    Takes a Spotify track ID.

    Returns:
      - str: direct audio download URL
      - dict: manifest dict ({"type":"manifest", "urls":[...], "codecs":"...", ...})
      - None on failure
    Ref: SpotiFLAC backend/tidal_alt.go GetAltDownloadURLFromSpotify()
    """
    for base in TIDAL_ALT_BASES:
        try:
            resp = requests.get(
                f"{base}/{spotify_track_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
                allow_redirects=False,
            )
            result = _parse_tidal_alt_response(resp)
            if result is not None:
                log.debug("tidal alt url OK for %s (base=%s)", spotify_track_id, base)
                return result
        except Exception as exc:
            log.debug("tidal alt proxy (%s): %s", base, exc)
    return None


def _get_tidal_alt_url_by_tidal_id(tidal_track_id: str) -> "str | dict | None":
    """
    Try to get a download URL/manifest from the Tidal Alt proxy using the Tidal track ID.
    Falls back when no Spotify ID is available (e.g. user provided a Tidal URL).
    Gap 5: rotates through all entries in TIDAL_ALT_BASES (SpotiFLAC backend/tidal_api_list.go).
    """
    for base in TIDAL_ALT_BASES:
        try:
            resp = requests.get(
                f"{base}/{tidal_track_id}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
                allow_redirects=False,
            )
            result = _parse_tidal_alt_response(resp)
            if result is not None:
                log.debug("tidal alt by tidal id OK (base=%s)", base)
                return result
        except Exception as exc:
            log.debug("tidal alt by tidal id (%s): %s", base, exc)
    return None


def _download_tidal_manifest(manifest: dict, out_path: Path) -> None:
    """
    Download a Tidal BTS V2 manifest by fetching each URL segment and
    concatenating bytes to out_path.
    Ref: SpotiFLAC backend/tidal.go DownloadFromManifest()
    Gap 2 implementation.
    """
    urls = manifest.get("urls") or []
    if not urls:
        raise RuntimeError("Tidal manifest has no URLs")

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
    """Derive a file extension from a Tidal manifest codecs/mimeType."""
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
    return ".flac"  # safe default


# ---------------------------------------------------------------------------
# Public entry point: get_track_info (Spotify → ISRC → multi-platform)
# ---------------------------------------------------------------------------

def _resolve_all_platforms(info: dict) -> dict:
    """
    Given an info dict that already has an ISRC, resolve it on Deezer, Qobuz,
    Tidal, and Amazon Music and add the results as extra keys.

    Resolution chain (each step fills in gaps left by previous steps):
      1. Deezer public ISRC API (always)
      2. Qobuz ISRC search (auto-scraped credentials, no account needed)
      3. Tidal ISRC API (if TIDAL_TOKEN set)
      3b. Tidal Alt — no token needed (uses Spotify track ID directly)
      4. Odesli / song.link API (no auth) — fills in any remaining gaps
      5. Songstats scrape (no auth) — last-resort Tidal/Amazon fallback
      6. MusicBrainz genre (non-blocking, best-effort)

    Gap 3: primary lookups (Deezer, Qobuz, Tidal, Tidal Alt, Odesli) now run
    concurrently via ThreadPoolExecutor (port of SpotiFLAC backend/analysis.go
    CheckTrackAvailability).
    """
    isrc = info.get("isrc") or ""

    info.update({
        "deezer_id": None, "deezer_url": None, "deezer_preview_url": None,
        "qobuz_id": None, "qobuz_url": None,
        "qobuz_bit_depth": None, "qobuz_sample_rate": None,
        "tidal_id": None, "tidal_url": None,
        "tidal_alt_url": None,
        "tidal_alt_available": False,
        "amazon_url": None,
    })

    if not isrc:
        return info

    spotify_id = info.get("track_id")

    # ── Primary lookups in parallel (Gap 3) ────────────────────────────────
    def _fetch_deezer() -> "dict | None":
        try:
            return _resolve_deezer(isrc)
        except Exception as exc:
            log.debug("parallel deezer: %s", exc)
            return None

    def _fetch_qobuz() -> "dict | None":
        try:
            return _resolve_qobuz_by_isrc(isrc)
        except Exception as exc:
            log.debug("parallel qobuz: %s", exc)
            return None

    def _fetch_tidal() -> "dict | None":
        try:
            return _resolve_tidal_by_isrc(isrc)
        except Exception as exc:
            log.debug("parallel tidal: %s", exc)
            return None

    def _fetch_tidal_alt() -> "str | dict | None":
        if not spotify_id:
            return None
        try:
            return _get_tidal_alt_url(spotify_id)
        except Exception as exc:
            log.debug("parallel tidal alt: %s", exc)
            return None

    odesli_input = (
        (f"https://open.spotify.com/track/{spotify_id}" if spotify_id else None)
        or info.get("deezer_url")
        or info.get("tidal_url")
        or info.get("qobuz_url")
    )

    def _fetch_odesli() -> dict:
        try:
            if odesli_input:
                return _resolve_via_odesli(odesli_input)
        except Exception as exc:
            log.debug("parallel odesli: %s", exc)
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        f_dz   = pool.submit(_fetch_deezer)
        f_qz   = pool.submit(_fetch_qobuz)
        f_td   = pool.submit(_fetch_tidal)
        f_talt = pool.submit(_fetch_tidal_alt)
        f_od   = pool.submit(_fetch_odesli)

        dz   = f_dz.result()
        qz   = f_qz.result()
        td   = f_td.result()
        talt = f_talt.result()
        od   = f_od.result()

    # ── 1. Deezer ──────────────────────────────────────────────────────────
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
    if qz:
        info["qobuz_id"]          = qz["id"]
        info["qobuz_url"]         = f"https://open.qobuz.com/track/{qz['id']}"
        info["qobuz_bit_depth"]   = qz.get("maximum_bit_depth") or qz.get("bit_depth") or 16
        info["qobuz_sample_rate"] = qz.get("maximum_sampling_rate") or qz.get("sampling_rate") or 44100
        log.debug("qobuz resolved: id=%s bd=%s sr=%s",
                  qz["id"], info["qobuz_bit_depth"], info["qobuz_sample_rate"])

    # ── 3. Tidal ───────────────────────────────────────────────────────────
    if td:
        info["tidal_id"]  = td["id"]
        info["tidal_url"] = f"https://tidal.com/browse/track/{td['id']}"
        log.debug("tidal resolved: id=%s", td["id"])

    # ── 3b. Tidal Alt ──────────────────────────────────────────────────────
    if talt:
        info["tidal_alt_url"] = talt
        log.debug("tidal alt resolved for track %s", spotify_id)
    elif spotify_id:
        # Mark as potentially available; will retry at download time
        info["tidal_alt_available"] = True
        log.debug("tidal alt not pre-resolved for %s; will try at download", spotify_id)

    # ── 4. Odesli — fills missing platform URLs (no auth) ─────────────────
    need_odesli = not info["tidal_url"] or not info["deezer_url"] or not info["qobuz_url"]
    if od and need_odesli:
        if od.get("deezer_url") and not info["deezer_url"]:
            info["deezer_url"] = od["deezer_url"]
        if od.get("qobuz_url") and not info["qobuz_url"]:
            info["qobuz_url"] = od["qobuz_url"]
        if od.get("tidal_url") and not info["tidal_url"]:
            info["tidal_url"] = od["tidal_url"]
        if od.get("amazon_url") and not info["amazon_url"]:
            info["amazon_url"] = od["amazon_url"]
        # Recover Spotify track ID when input was Tidal/Qobuz/Amazon
        if od.get("spotify_url") and not info.get("track_id"):
            sp_id = parse_spotify_track_id(od["spotify_url"])
            if sp_id:
                info["track_id"] = sp_id
                log.debug("spotify track ID recovered from odesli: %s", sp_id)
                if not info.get("tidal_alt_url"):
                    tidal_alt_url = _get_tidal_alt_url(sp_id)
                    if tidal_alt_url:
                        info["tidal_alt_url"] = tidal_alt_url
                    else:
                        info["tidal_alt_available"] = True

    # ── 5. Songstats — last-resort (no auth) ──────────────────────────────
    if isrc and (not info["tidal_url"] or not info["amazon_url"]):
        sg = _resolve_via_songstats(isrc)
        if sg.get("tidal_url") and not info["tidal_url"]:
            info["tidal_url"] = sg["tidal_url"]
        if sg.get("deezer_url") and not info["deezer_url"]:
            info["deezer_url"] = sg["deezer_url"]
        if sg.get("amazon_url") and not info["amazon_url"]:
            info["amazon_url"] = sg["amazon_url"]

    # ── 5b. Deezer ISRC → URL — third fallback if still no Deezer URL ──────
    # Ref: SpotiFLAC songlink.go lookupDeezerTrackURLByISRC()
    if isrc and not info["deezer_url"]:
        dz_url = _deezer_url_from_isrc(isrc)
        if dz_url:
            info["deezer_url"] = dz_url
            log.debug("deezer url from isrc fallback: %s", dz_url)

    # ── 6. MusicBrainz genre (non-blocking, best-effort) ──────────────────
    if isrc and not info.get("genre"):
        genre = _musicbrainz_genre(isrc)
        if genre:
            info["genre"] = genre

    return info


def get_track_info(track_id: str) -> dict:
    """
    Fetch Spotify track metadata and resolve ISRC on Deezer / Qobuz / Tidal.

    Returns a dict with:
      title, artists (list), album, release_date, cover_url,
      track_number, disc_number, isrc, upc, lyrics,
      deezer_id, deezer_url, deezer_preview_url,
      qobuz_id, qobuz_url,
      tidal_id, tidal_url, tidal_alt_url

    Gap 6: results are cached in-process for up to 10 minutes (LRU, 256 entries)
    — port of SpotiFLAC backend/recent_fetches.go.
    """
    # --- Phase 0a: In-process LRU metadata cache (Gap 6) ---
    cached = _cache_get_track_info(track_id)
    if cached is not None:
        log.debug("track info cache hit for %s", track_id)
        return cached

    info: dict = {}

    # --- Phase 0b: Check ISRC disk cache (skip metadata fetch if ISRC known) ---
    cached_isrc = _get_cached_isrc(track_id)
    if cached_isrc:
        log.debug("isrc cache hit for track %s: %s", track_id, cached_isrc)
        info = {
            "title": "", "artists": [], "album": "",
            "release_date": "", "cover_url": "",
            "track_number": 1, "disc_number": 1,
            "isrc": cached_isrc,
        }
        info["track_id"] = track_id
        result = _resolve_all_platforms(info)
        _cache_set_track_info(track_id, result)
        return result

    # --- Phase 1: Spotify metadata via GraphQL (primary) ---
    _internal_raw: dict | None = None
    try:
        raw = _fetch_track_graphql(track_id)
        info = _parse_graphql_track(raw)
        log.debug("graphql meta OK  track=%s  title=%r", track_id, info.get("title"))
    except Exception as exc:
        log.warning("graphql meta failed (%s) — trying spclient", exc)
        try:
            _internal_raw = _fetch_internal_meta(track_id)
            info = _parse_internal(_internal_raw)
            log.debug("internal meta OK  track=%s  title=%r", track_id, info.get("title"))
        except Exception as exc2:
            log.warning("internal meta failed (%s) — trying public API", exc2)
            try:
                raw = _fetch_public_meta(track_id)
                info = _parse_public(raw)
                log.debug("public meta OK  track=%s  title=%r", track_id, info.get("title"))
            except Exception as exc3:
                log.error("public meta also failed: %s", exc3)
                info = {
                    "title": "", "artists": [], "album": "",
                    "release_date": "", "cover_url": "",
                    "track_number": 1, "disc_number": 1, "isrc": None,
                }

    info["track_id"] = track_id

    # Upgrade cover art to maximum resolution (Ref: SpotiFLAC backend/cover.go)
    if info.get("cover_url"):
        info["cover_url"] = _upgrade_spotify_cover_url(info["cover_url"])

    # ISRC via Soundplate if still missing
    if not info.get("isrc"):
        info["isrc"] = _isrc_soundplate(track_id)

    # Persist ISRC to disk cache for future lookups
    if info.get("isrc"):
        _put_cached_isrc(track_id, info["isrc"])

    # --- Phase 1b: UPC from album GID via spclient (Ref: SpotiFLAC isrc_finder.go) ---
    if _internal_raw and not info.get("upc"):
        try:
            album_gid_bytes = (_internal_raw.get("album") or {}).get("gid")
            if album_gid_bytes:
                # gid may be bytes or a hex string; convert to hex
                if isinstance(album_gid_bytes, (bytes, bytearray)):
                    album_gid_hex = album_gid_bytes.hex()
                else:
                    album_gid_hex = str(album_gid_bytes).lower()
                album_meta_resp = requests.get(
                    f"https://spclient.wg.spotify.com/metadata/4/album/{album_gid_hex}?market=from_token",
                    headers=_auth_headers(),
                    timeout=10,
                )
                if album_meta_resp.ok:
                    album_meta = album_meta_resp.json()
                    for eid in album_meta.get("external_id") or []:
                        if eid.get("type") == "upc":
                            info["upc"] = eid.get("id", "")
                            log.debug("upc from album gid: %s", info["upc"])
                            break
        except Exception as exc:
            log.debug("album gid upc fetch: %s", exc)

    # --- Phase 1c: Deezer ISRC fallback (if still no ISRC) ---
    # If we got a Deezer URL from a previous lookup but no ISRC, extract from Deezer
    # (will be populated after _resolve_all_platforms if needed — deferred below)

    # --- Phase 2: multi-platform resolution ---
    info = _resolve_all_platforms(info)

    # --- Phase 2b: ISRC from Deezer URL if still no ISRC after platform resolution ---
    if not info.get("isrc") and info.get("deezer_url"):
        dz_isrc = _deezer_isrc_from_url(info["deezer_url"])
        if dz_isrc:
            info["isrc"] = dz_isrc
            _put_cached_isrc(track_id, dz_isrc)
            log.debug("isrc from deezer url: %s", dz_isrc)

    # --- Phase 3: Fetch lyrics in background (non-blocking) ---
    title   = info.get("title", "")
    artists = info.get("artists") or []
    album   = info.get("album", "")
    if title and artists:
        def _bg_lyrics() -> None:
            try:
                artist_str = artists[0] if artists else ""
                lyrics = get_lyrics(title, artist_str, album)
                if lyrics:
                    info["lyrics"] = lyrics
                    log.debug("lyrics fetched for %r", title)
            except Exception as exc:
                log.debug("lyrics fetch: %s", exc)
        t = threading.Thread(target=_bg_lyrics, daemon=True)
        t.start()
        t.join(timeout=15)  # wait up to 15s so lyrics are available before download

    # Cache result for future calls (Gap 6)
    _cache_set_track_info(track_id, info)
    return info


# ---------------------------------------------------------------------------
# Public entry point: get_tidal_track_info
# ---------------------------------------------------------------------------

def get_tidal_track_info(track_id: str) -> dict:
    """
    Fetch Tidal track metadata. Works with or without TIDAL_TOKEN.
    If TIDAL_TOKEN is set, uses the Tidal API for full metadata.
    Otherwise, resolves via Odesli (song.link free API) to get ISRC
    and all platform URLs, then uses Tidal Alt proxy for download.
    """
    tidal_url = f"https://tidal.com/browse/track/{track_id}"

    # --- Path 1: TIDAL_TOKEN available — use API ---
    if TIDAL_TOKEN:
        data = _get_tidal_track(track_id)
        if data:
            info = _parse_tidal_track(data)
            info["track_id"] = None
            info["tidal_id"] = track_id
            info["tidal_url"] = tidal_url
            return _resolve_all_platforms(info)

    # --- Path 2: No token — resolve via Odesli ---
    log.info("No TIDAL_TOKEN; resolving Tidal track %s via Odesli", track_id)
    od = _resolve_via_odesli(tidal_url)

    # Try to get ISRC from Deezer (free public API) if we got a Deezer URL
    isrc = ""
    if od.get("deezer_url"):
        isrc = _deezer_isrc_from_url(od["deezer_url"]) or ""

    # Build a minimal info dict from whatever Odesli gave us
    info: dict = {
        "title": "",
        "artists": [],
        "album": "",
        "release_date": "",
        "cover_url": "",
        "track_number": 1,
        "disc_number": 1,
        "isrc": isrc,
        "track_id": None,            # no Spotify ID known yet
        "tidal_id": track_id,
        "tidal_url": tidal_url,
        "tidal_alt_url": None,
        "tidal_alt_available": True,  # signal: attempt at download time
    }

    # Pre-populate platform URLs from Odesli
    info.update({
        "deezer_id": None,
        "deezer_url": od.get("deezer_url"),
        "deezer_preview_url": None,
        "qobuz_id": None,
        "qobuz_url": od.get("qobuz_url"),
        "qobuz_bit_depth": None,
        "qobuz_sample_rate": None,
        "amazon_url": od.get("amazon_url"),
    })

    # Recover Spotify track ID from Odesli result if available
    if od.get("spotify_url"):
        sp_id = parse_spotify_track_id(od["spotify_url"])
        if sp_id:
            info["track_id"] = sp_id
            log.debug("spotify track ID recovered from odesli for tidal track: %s", sp_id)
            # Pre-resolve Tidal Alt with the Spotify ID
            tidal_alt = _get_tidal_alt_url(sp_id)
            if tidal_alt:
                info["tidal_alt_url"] = tidal_alt
                info["tidal_alt_available"] = False

    # If we got a Qobuz URL from Odesli, extract the Qobuz track ID and metadata
    if info.get("qobuz_url"):
        qobuz_id = parse_qobuz_track_id(info["qobuz_url"])
        if qobuz_id:
            info["qobuz_id"] = qobuz_id
            try:
                qz_data = _get_qobuz_track(qobuz_id)
                if qz_data:
                    parsed = _parse_qobuz_track(qz_data)
                    for k in ("title", "artists", "album", "release_date", "cover_url",
                              "track_number", "disc_number", "isrc"):
                        if parsed.get(k) and not info.get(k):
                            info[k] = parsed[k]
                    info["qobuz_bit_depth"]   = qz_data.get("maximum_bit_depth", 16)
                    info["qobuz_sample_rate"] = qz_data.get("maximum_sampling_rate", 44100)
            except Exception as exc:
                log.debug("qobuz metadata for tidal track: %s", exc)

    # If we got a Deezer URL, fetch Deezer metadata for title/artist
    if info.get("deezer_url") and not info.get("title"):
        try:
            dz_track_id = re.search(r'/track/(\d+)', info["deezer_url"])
            if dz_track_id:
                resp = requests.get(
                    f"https://api.deezer.com/track/{dz_track_id.group(1)}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                if resp.ok:
                    dz = resp.json()
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
                    if not info.get("isrc"):
                        info["isrc"] = dz.get("isrc", "")
                    if not info.get("deezer_preview_url"):
                        info["deezer_preview_url"] = dz.get("preview", "")
        except Exception as exc:
            log.debug("deezer metadata for tidal track: %s", exc)

    # If we still have no title, the track is unresolvable
    if not info.get("title") and not info.get("isrc"):
        raise RuntimeError(
            f"Could not resolve Tidal track {track_id!r}. "
            "No TIDAL_TOKEN and Odesli/Deezer returned no metadata."
        )

    return info


# ---------------------------------------------------------------------------
# Public entry point: get_qobuz_track_info
# ---------------------------------------------------------------------------

def get_qobuz_track_info(track_id: str) -> dict:
    """
    Fetch Qobuz track metadata and resolve ISRC on Deezer / Tidal.
    Credentials are auto-scraped from open.qobuz.com — no account needed.
    """
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
# Lyrics fetching from lrclib.net (Ref: SpotiFLAC backend/lyrics.go)
# ---------------------------------------------------------------------------

_LRCLIB_BASE = "https://lrclib.net/api"
_LRCLIB_UA   = "Tele2Rub/1.0 (https://github.com/xshayank/Tele2Rub)"


def _fetch_lyrics_lrclib(track: str, artist: str, album: str = "", duration: int = 0) -> dict | None:
    """
    Fetch lyrics from lrclib.net.
    Returns dict with keys: synced_lyrics, plain_lyrics, is_synced — or None on failure.
    Ref: SpotiFLAC backend/lyrics.go FetchLyricsWithMetadata()
    """
    params: dict = {"artist_name": artist, "track_name": track}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration

    try:
        resp = requests.get(
            f"{_LRCLIB_BASE}/get",
            params=params,
            headers={"User-Agent": _LRCLIB_UA},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            synced = data.get("syncedLyrics") or ""
            plain  = data.get("plainLyrics") or ""
            if synced or plain:
                return {"synced_lyrics": synced, "plain_lyrics": plain, "is_synced": bool(synced)}
    except Exception as exc:
        log.debug("lrclib get: %s", exc)

    # Fallback: search endpoint
    try:
        resp = requests.get(
            f"{_LRCLIB_BASE}/search",
            params={"artist_name": artist, "track_name": track},
            headers={"User-Agent": _LRCLIB_UA},
            timeout=10,
        )
        if resp.ok:
            results = resp.json()
            if results:
                for item in results:
                    if item.get("syncedLyrics"):
                        return {
                            "synced_lyrics": item["syncedLyrics"],
                            "plain_lyrics":  item.get("plainLyrics", ""),
                            "is_synced":     True,
                        }
                item = results[0]
                return {
                    "synced_lyrics": "",
                    "plain_lyrics":  item.get("plainLyrics", ""),
                    "is_synced":     False,
                }
    except Exception as exc:
        log.debug("lrclib search: %s", exc)

    return None


def get_lyrics(track_name: str, artist_name: str, album_name: str = "", duration: int = 0) -> str | None:
    """
    Fetch lyrics for a track. Returns LRC-formatted string (synced if available,
    else plain text), or None if not found.
    Ref: SpotiFLAC backend/lyrics.go FetchLyricsAllSources() / ConvertToLRC()
    """
    result = _fetch_lyrics_lrclib(track_name, artist_name, album_name, duration)
    if not result:
        return None
    if result["is_synced"] and result["synced_lyrics"]:
        return result["synced_lyrics"]
    if result["plain_lyrics"]:
        return result["plain_lyrics"]
    return None


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
            TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, APIC, TSRC, TCON, COMM,
            USLT, TXXX,
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
        if info.get("albumartist") or info.get("album_artist"):
            tags.add(TPE2(encoding=3, text=info.get("albumartist") or info.get("album_artist") or ""))
        if info.get("genre"):
            tags.add(TCON(encoding=3, text=info["genre"]))
        if info.get("isrc"):
            tags.add(COMM(encoding=3, lang="eng", desc="", text=info.get("isrc", "")))
        if info.get("upc"):
            tags.add(TXXX(encoding=3, desc="UPC", text=info["upc"]))
        if info.get("lyrics"):
            tags.add(USLT(encoding=3, lang="eng", desc="", text=info["lyrics"]))
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
        if info.get("albumartist") or info.get("album_artist"):
            audio["albumartist"] = info.get("albumartist") or info.get("album_artist") or ""
        if info.get("genre"):
            audio["genre"] = info["genre"]
        if info.get("comment"):
            audio["comment"] = info["comment"]
        if info.get("upc"):
            audio["upc"] = [info["upc"]]
        if info.get("lyrics"):
            audio["lyrics"] = [info["lyrics"]]
        if cover_data:
            pic = Picture()
            pic.type = 3  # cover front
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        log.debug("FLAC tags written to %s", filepath.name)

    elif ext == ".m4a":
        # Gap 4: M4A/AAC tagging via mutagen.mp4
        # Ref: SpotiFLAC backend/metadata.go EmbedMetadata() — uses ffmpeg -metadata for M4A
        try:
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(filepath))
            audio["\xa9nam"] = [info.get("title", "")]
            audio["\xa9ART"] = [", ".join(info.get("artists", []))]
            audio["\xa9alb"] = [info.get("album", "")]
            audio["\xa9day"] = [str(info.get("release_date", ""))]
            trkn = info.get("track_number", 1)
            trkn_total = info.get("track_total", 0)
            audio["trkn"] = [(int(trkn), int(trkn_total))]
            disk = info.get("disc_number", 1)
            audio["disk"] = [(int(disk), 0)]
            if info.get("isrc"):
                # Store ISRC as iTunes freeform tag (standard practice)
                audio["----:com.apple.iTunes:ISRC"] = [info["isrc"].encode()]
            if info.get("albumartist") or info.get("album_artist"):
                audio["aART"] = [info.get("albumartist") or info.get("album_artist") or ""]
            if info.get("genre"):
                audio["\xa9gen"] = [info["genre"]]
            if info.get("lyrics"):
                audio["\xa9lyr"] = [info["lyrics"]]
            if cover_data:
                audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            log.debug("MP4 tags written to %s", filepath.name)
        except Exception as exc:
            log.warning("mutagen MP4 tagging failed for %s: %s — trying ffmpeg remux", filepath.name, exc)
            # Fallback: ffmpeg remux with -metadata flags
            import shutil
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                meta_args: list[str] = []
                for k, v in [
                    ("title",  info.get("title", "")),
                    ("artist", ", ".join(info.get("artists", []))),
                    ("album",  info.get("album", "")),
                    ("date",   str(info.get("release_date", ""))),
                    ("track",  str(info.get("track_number", 1))),
                ]:
                    if v:
                        meta_args += ["-metadata", f"{k}={v}"]
                if info.get("isrc"):
                    meta_args += ["-metadata", f"ISRC={info['isrc']}"]
                tmp_path = filepath.with_suffix(".tagged.m4a")
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-i", str(filepath)] + meta_args +
                        ["-c", "copy", str(tmp_path)],
                        capture_output=True, timeout=60,
                    )
                    if tmp_path.exists() and tmp_path.stat().st_size > 0:
                        filepath.unlink(missing_ok=True)
                        tmp_path.rename(filepath)
                        log.debug("ffmpeg M4A metadata remux OK: %s", filepath.name)
                    else:
                        tmp_path.unlink(missing_ok=True)
                except Exception as exc2:
                    log.warning("ffmpeg M4A remux also failed: %s", exc2)

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
    "https://qobuz2.spotbye.qzz.io/api/track/{id}?quality={q}",
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

# ---------------------------------------------------------------------------
# Provider priority / success tracking (Ref: SpotiFLAC backend/provider_priority.go)
# ---------------------------------------------------------------------------

_PROVIDER_STATS_FILE = Path(tempfile.gettempdir()) / "tele2rub" / "provider_stats.json"
_provider_stats_lock = threading.Lock()


def _load_provider_stats() -> dict:
    try:
        if _PROVIDER_STATS_FILE.exists():
            return json.loads(_PROVIDER_STATS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_provider_stats(stats: dict) -> None:
    try:
        _PROVIDER_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROVIDER_STATS_FILE.write_text(json.dumps(stats, indent=2))
    except Exception:
        pass


def _record_provider_outcome(service: str, provider: str, success: bool) -> None:
    with _provider_stats_lock:
        stats = _load_provider_stats()
        key = f"{service}|{provider}"
        entry = stats.get(key, {"success": 0, "failure": 0, "last_success": 0})
        if success:
            entry["success"] = entry.get("success", 0) + 1
            entry["last_success"] = time.time()
        else:
            entry["failure"] = entry.get("failure", 0) + 1
        stats[key] = entry
        _save_provider_stats(stats)


def _prioritize_providers(service: str, providers: list) -> list:
    """Sort providers by most recent success. Ref: SpotiFLAC backend/provider_priority.go"""
    try:
        stats = _load_provider_stats()

        def score(p: str) -> float:
            entry = stats.get(f"{service}|{p}", {})
            return entry.get("last_success", 0.0)

        return sorted(providers, key=score, reverse=True)
    except Exception:
        return providers


def _get_qobuz_stream_url(track_id: str, quality_num: int) -> str | None:
    """
    Try each proxy API in order and return the first signed stream URL found.
    Returns *None* if all proxies fail for this (track_id, quality_num) pair.
    """
    ordered = _prioritize_providers("qobuz", list(_QOBUZ_STREAM_PROXIES))
    for template in ordered:
        url = template.format(id=track_id, q=quality_num)
        try:
            # Try without following redirects first to catch 30x audio URLs
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
                allow_redirects=False,
            )

            # Handle HTTP redirects (some proxies redirect to the CDN directly)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if location.startswith("http") and any(
                    ext in location for ext in (".flac", ".mp3", ".m4a", "audio", "stream")
                ):
                    log.debug("qobuz stream via %s (redirect)", url)
                    _record_provider_outcome("qobuz", template, True)
                    return location

            if not resp.ok:
                log.debug("qobuz proxy %s → HTTP %d", url, resp.status_code)
                _record_provider_outcome("qobuz", template, False)
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
                        _record_provider_outcome("qobuz", template, True)
                        return str(stream_url)
                except Exception:
                    pass

            # Plain-text URL
            if "text/plain" in ct:
                url_candidate = resp.text.strip()
                if url_candidate.startswith("http"):
                    log.debug("qobuz stream via %s (plain)", url)
                    _record_provider_outcome("qobuz", template, True)
                    return url_candidate

            text = resp.text.strip()
            if text.startswith("http"):
                log.debug("qobuz stream via %s (text)", url)
                _record_provider_outcome("qobuz", template, True)
                return text

            # The proxy might have redirected to the actual CDN URL (after allow_redirects=True)
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
                    _record_provider_outcome("qobuz", template, True)
                    return resp.url

            _record_provider_outcome("qobuz", template, False)

        except Exception as exc:
            log.debug("qobuz proxy %s error: %s", url, exc)
            _record_provider_outcome("qobuz", template, False)

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


# ---------------------------------------------------------------------------
# Authenticated Qobuz fallback (Gap 8)
# Port of SpotiFLAC backend/qobuz_api.go userLogin() + doQobuzSignedRequest()
# Used only when QOBUZ_EMAIL + QOBUZ_PASSWORD are set and all proxy APIs fail.
# ---------------------------------------------------------------------------

_qobuz_auth_token: "str | None" = None
_qobuz_auth_lock  = threading.Lock()


def _qobuz_auth_login() -> "str | None":
    """
    Attempt an authenticated Qobuz login and return a user auth token.
    Returns None on failure. Caches the token module-globally.
    Ref: SpotiFLAC backend/qobuz_api.go userLogin()
    """
    global _qobuz_auth_token
    if not QOBUZ_EMAIL or not QOBUZ_PASSWORD:
        return None
    with _qobuz_auth_lock:
        if _qobuz_auth_token:
            return _qobuz_auth_token
        try:
            creds = _get_qobuz_creds()
            app_id     = creds["app_id"]
            app_secret = creds["app_secret"]
            resp = requests.post(
                f"{_QOBUZ_API_BASE}/user/login",
                params={
                    "username":  QOBUZ_EMAIL,
                    # Qobuz API requires MD5 of the plaintext password (wire protocol requirement)
                    "password":  hashlib.md5(QOBUZ_PASSWORD.encode()).hexdigest(),  # noqa: S324
                    "app_id":    app_id,
                },
                headers={"User-Agent": _QOBUZ_UA, "X-App-Id": app_id},
                timeout=15,
            )
            if resp.ok:
                data = resp.json()
                token = (data.get("user_auth_token") or
                         (data.get("user") or {}).get("auth_token") or "")
                if token:
                    _qobuz_auth_token = token
                    log.info("qobuz authenticated login succeeded")
                    return token
            log.warning("qobuz auth login failed: HTTP %d", resp.status_code)
        except Exception as exc:
            log.warning("qobuz auth login error: %s", exc)
    return None


def _get_qobuz_stream_url_auth(track_id: str, quality_num: int) -> "str | None":
    """
    Authenticated Qobuz stream URL via user auth token + signed request.
    Ref: SpotiFLAC backend/qobuz_api.go doQobuzSignedRequest()
    Gap 8: called only when both QOBUZ_EMAIL and QOBUZ_PASSWORD are set
    and all proxy API attempts have failed.
    """
    token = _qobuz_auth_login()
    if not token:
        return None
    try:
        creds = _get_qobuz_creds()
        app_id     = creds["app_id"]
        app_secret = creds["app_secret"]
        ts = str(int(time.time()))
        # Ref: SpotiFLAC backend/qobuz_api.go doQobuzSignedRequest()
        # The Qobuz API requires MD5 signature: concat endpoint suffix +
        # sorted param name+value pairs + timestamp + app_secret (API protocol).
        # MD5 here is an API wire format requirement, not a password storage choice.
        sig_params = (
            "trackgetFileUrl"
            + f"format_id{quality_num}"
            + "intentstream"
            + f"track_id{track_id}"
            + ts
            + app_secret
        )
        sig = hashlib.md5(sig_params.encode()).hexdigest()  # noqa: S324 — Qobuz API protocol
        resp = requests.get(
            f"{_QOBUZ_API_BASE}/track/getFileUrl",
            params={
                "track_id":        track_id,
                "format_id":       quality_num,
                "intent":          "stream",
                "request_ts":      ts,
                "request_sig":     sig,
                "app_id":          app_id,
                "user_auth_token": token,
            },
            headers={"User-Agent": _QOBUZ_UA, "X-App-Id": app_id, "X-User-Auth-Token": token},
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            url = data.get("url") or data.get("stream_url") or ""
            if url.startswith("http"):
                log.debug("qobuz auth stream url OK for track %s quality %s", track_id, quality_num)
                return url
    except Exception as exc:
        log.warning("qobuz auth stream url error: %s", exc)
    return None


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

    # Gap 8: authenticated Qobuz fallback when all proxy APIs failed
    # Ref: SpotiFLAC backend/qobuz_api.go userLogin() + doQobuzSignedRequest()
    if not stream_url and QOBUZ_EMAIL and QOBUZ_PASSWORD:
        log.info("qobuz proxy failed; trying authenticated fallback for track %s", track_id)
        for qnum in quality_nums:
            stream_url = await loop.run_in_executor(
                None, _get_qobuz_stream_url_auth, str(track_id), qnum
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
# Amazon Music URL normalization and download
# Ref: SpotiFLAC backend/songlink.go normalizeAmazonMusicURL()
# ---------------------------------------------------------------------------

_AMAZON_ALBUM_TRACK_RE = re.compile(r'/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})')
_AMAZON_TRACK_RE       = re.compile(r'/tracks/(B[0-9A-Z]{9})')


def _normalize_amazon_url(raw_url: str) -> str:
    """
    Normalize an Amazon Music URL to the canonical track URL form.
    Ref: SpotiFLAC backend/songlink.go normalizeAmazonMusicURL()
    """
    url = (raw_url or "").strip()
    if not url:
        return ""

    if "trackAsin=" in url:
        m = re.search(r'trackAsin=([A-Z0-9]{10})', url)
        if m:
            return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"

    m = _AMAZON_ALBUM_TRACK_RE.search(url)
    if m:
        return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"

    m = _AMAZON_TRACK_RE.search(url)
    if m:
        return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"

    return ""


_AMAZON_PROXY_APIS = [
    "https://afkar.xyz/api/track/{asin}",
    "https://amazon.spotbye.qzz.io/api/track/{asin}",
]


def _get_amazon_stream_url(asin: str) -> tuple[str, str]:
    """
    Returns (stream_url, decryption_key) from the Amazon proxy API.
    decryption_key is "" if no decryption is needed.
    """
    for template in _AMAZON_PROXY_APIS:
        url = template.format(asin=asin)
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.ok:
                data = resp.json()
                stream_url = data.get("streamUrl") or data.get("url") or data.get("link") or ""
                decryption_key = data.get("decryptionKey") or data.get("key") or ""
                if stream_url.startswith("http"):
                    return stream_url, decryption_key
        except Exception as exc:
            log.debug("amazon proxy %s: %s", url, exc)
    return "", ""


def _extract_amazon_asin(amazon_url: str) -> str | None:
    """Extract B0XXXXXXXXX ASIN from an Amazon Music URL."""
    m = re.search(r'(B[0-9A-Z]{9})', amazon_url)
    return m.group(1) if m else None


def _download_raw_stream(url: str, dest: Path) -> None:
    """Download a file from a direct URL to dest (blocking)."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(65536):
            if chunk:
                f.write(chunk)


async def _convert_or_rename_amazon(
    raw_path: Path, download_dir: Path, stem: str, decryption_key: str = ""
) -> Path:
    """
    Optionally decrypt, then convert/rename a raw Amazon download to FLAC.

    Gap 1 — port of SpotiFLAC backend/amazon.go:
      When decryption_key is non-empty, first runs:
        ffmpeg -decryption_key {key} -i {raw} -c copy {decrypted}
      to produce a clear intermediate, then probes and converts to FLAC.
      When the key is empty, behaves as before.
    """
    import shutil
    ffmpeg = shutil.which("ffmpeg")

    work_path = raw_path  # the file we'll probe / convert

    # ── Step 1 (Gap 1): decrypt if a key was provided ─────────────────────
    if decryption_key and ffmpeg:
        decrypted_path = raw_path.with_suffix(".dec.mp4")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y",
            "-decryption_key", decryption_key,
            "-i", str(raw_path),
            "-c", "copy",
            str(decrypted_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if decrypted_path.exists() and decrypted_path.stat().st_size > 0:
            raw_path.unlink(missing_ok=True)
            work_path = decrypted_path
            log.debug("amazon decryption OK: %s", decrypted_path.name)
        else:
            log.warning("amazon decryption produced empty file; trying without key")
            decrypted_path.unlink(missing_ok=True)

    # ── Step 2: probe codec so we can decide copy vs. transcode ───────────
    codec = ""
    if ffmpeg:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                probe = await asyncio.create_subprocess_exec(
                    ffprobe, "-v", "quiet",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(work_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await probe.communicate()
                codec = stdout.decode().strip().lower()
                log.debug("amazon codec probe: %s", codec)
            except Exception as exc:
                log.debug("ffprobe failed: %s", exc)

    # ── Step 3: rename to .flac (already FLAC) or convert ─────────────────
    if ffmpeg:
        out_flac = download_dir / f"{stem}.flac"
        if codec == "flac":
            # Already FLAC — just copy container
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-i", str(work_path), "-c", "copy", str(out_flac),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            # Transcode to FLAC
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-i", str(work_path), "-vn", "-c:a", "flac", str(out_flac),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        await proc.wait()
        if out_flac.exists() and out_flac.stat().st_size > 0:
            work_path.unlink(missing_ok=True)
            return out_flac

    # ffmpeg unavailable or conversion failed — keep as .m4a
    out_m4a = work_path.with_suffix(".m4a")
    if work_path != out_m4a:
        work_path.rename(out_m4a)
    return out_m4a


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

async def download_track(info: dict, download_dir: Path, ytdlp_bin: str) -> Path:
    """
    Download a single track to *download_dir* and embed metadata.
    Returns the Path of the downloaded file.

    Priority:
      1. Qobuz FLAC  — via proxy stream APIs (no credentials required), if qobuz_id available
      1b. Tidal Alt  — no credentials; uses Spotify track ID directly
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
            log.warning("qobuz proxy download failed, trying Tidal Alt/Deezer/YTMusic: %s", exc)

    # ── 1b. Tidal Alt (no credentials) ────────────────────────────────────
    spotify_id = info.get("track_id")
    if not qobuz_id and spotify_id:
        try:
            tidal_result = info.get("tidal_alt_url") or _get_tidal_alt_url(spotify_id)
            if tidal_result:
                loop = asyncio.get_event_loop()
                # Gap 2: handle V2 manifest (SpotiFLAC backend/tidal.go DownloadFromManifest)
                if isinstance(tidal_result, dict) and tidal_result.get("type") == "manifest":
                    ext = _ext_from_manifest(tidal_result)
                    fp = download_dir / f"{safe}{ext}"
                    await loop.run_in_executor(None, _download_tidal_manifest, tidal_result, fp)
                else:
                    direct_url = str(tidal_result)
                    from urllib.parse import urlparse as _urlparse
                    ext = Path(_urlparse(direct_url).path).suffix.lower() or ".flac"
                    fp = download_dir / f"{safe}{ext}"
                    await loop.run_in_executor(None, _download_raw_stream, direct_url, fp)
                try:
                    embed_metadata(fp, info)
                except Exception as exc:
                    log.warning("metadata embed failed: %s", exc)
                return fp
        except Exception as exc:
            log.warning("tidal alt download failed, trying Deezer/YTMusic: %s", exc)

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
        return "🇶🇿 Qobuz FLAC"
    if info.get("tidal_alt_url") or info.get("tidal_alt_available"):
        return "🇳🇴 Tidal FLAC (keyless)"
    if DEEZER_ARL and info.get("deezer_url"):
        return "🇫🇷 Deezer FLAC"
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
    Fetch metadata and all tracks for a Spotify playlist via GraphQL.
    Returns (playlist_info_dict, list_of_track_ids).
    Falls back to the public REST API if GraphQL fails.
    """
    try:
        all_track_ids: list = []
        playlist_info: dict = {}
        offset = 0
        limit = 300

        while True:
            data = _fetch_playlist_graphql_page(playlist_id, offset, limit)
            playlist_v2 = ((data.get("data") or {}).get("playlistV2") or {})

            # Parse playlist info on the first page
            if not playlist_info:
                owner_data = ((playlist_v2.get("ownerV2") or {}).get("data") or {})
                owner_name = owner_data.get("name") or owner_data.get("username") or ""
                image_items = ((playlist_v2.get("images") or {}).get("items") or [])
                cover_url = ""
                if image_items:
                    img_sources = (image_items[0].get("sources") or [])
                    if img_sources:
                        cover_url = img_sources[0].get("url", "")
                playlist_info = {
                    "name":      playlist_v2.get("name", "playlist"),
                    "owner":     owner_name,
                    "cover_url": cover_url,
                }

            content = (playlist_v2.get("content") or {})
            total_count = content.get("totalCount") or 0
            items = (content.get("items") or [])

            for item in items:
                # item["itemV2"]["data"] is the track object directly —
                # there is no "trackUnion" wrapper inside playlist items
                # (that key only appears in standalone getTrack responses).
                item_data = ((item.get("itemV2") or {}).get("data") or {})
                # Skip non-track entries such as podcast episodes
                typename = item_data.get("__typename", "")
                if typename and typename != "Track":
                    continue
                tid = item_data.get("id") or ""
                if not tid:
                    uri = item_data.get("uri") or ""
                    parts = uri.split(":")
                    if len(parts) == 3 and parts[0] == "spotify" and parts[1] == "track":
                        tid = parts[2]
                if tid:
                    all_track_ids.append(tid)

            # Stop when there are no more items, or when we have collected
            # all tracks according to totalCount (guard against totalCount=0
            # so that we do not stop after the very first empty page).
            if not items or (total_count > 0 and len(all_track_ids) >= total_count):
                break
            offset += limit

        log.info("playlist %s (graphql): %d tracks", playlist_id, len(all_track_ids))
        return playlist_info, all_track_ids

    except Exception as exc:
        log.warning("graphql playlist fetch failed (%s) — falling back to REST API", exc)
        return _get_spotify_playlist_tracks_rest(playlist_id)


def _get_spotify_playlist_tracks_rest(playlist_id: str) -> tuple[dict, list]:
    """Fallback: fetch playlist tracks via Spotify's public REST API."""
    headers = _auth_headers()

    pl_resp = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        params={"fields": "name,owner,images"},
        timeout=15,
    )
    pl_resp.raise_for_status()
    pl_data = pl_resp.json()

    playlist_info = {
        "name":      pl_data.get("name", "playlist"),
        "owner":     (pl_data.get("owner") or {}).get("display_name", ""),
        "cover_url": ((pl_data.get("images") or [{}])[0]).get("url", ""),
    }

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

    log.info("playlist %s (rest): %d tracks", playlist_id, len(track_ids))
    return playlist_info, track_ids


def get_spotify_album_tracks(album_id: str) -> tuple[dict, list]:
    """
    Fetch metadata and all tracks for a Spotify album via GraphQL.
    Returns (album_info_dict, list_of_track_ids).
    Falls back to the public REST API if GraphQL fails.
    """
    try:
        all_track_ids: list = []
        album_info: dict = {}
        offset = 0
        limit = 300

        while True:
            data = _fetch_album_graphql_page(album_id, offset, limit)
            album_union = ((data.get("data") or {}).get("albumUnion") or {})

            # Parse album info on the first page
            if not album_info:
                artist_items = ((album_union.get("artists") or {}).get("items") or [])
                artists = [
                    (item.get("profile") or {}).get("name", "")
                    for item in artist_items
                    if (item.get("profile") or {}).get("name")
                ]
                iso = ((album_union.get("date") or {}).get("isoString") or "")
                release_date = iso[:10] if iso else ""
                sources = ((album_union.get("coverArt") or {}).get("sources") or [])
                cover_url = sources[0].get("url", "") if sources else ""
                album_info = {
                    "name":         album_union.get("name", "album"),
                    "artists":      artists,
                    "release_date": release_date,
                    "cover_url":    cover_url,
                    "total_tracks": ((album_union.get("tracksV2") or {}).get("totalCount") or 0),
                }

            tracks_v2 = (album_union.get("tracksV2") or {})
            total_count = tracks_v2.get("totalCount") or 0
            items = (tracks_v2.get("items") or [])

            for item in items:
                track = (item.get("track") or {})
                tid = track.get("id")
                if not tid:
                    uri = track.get("uri") or ""
                    parts = uri.split(":")
                    if len(parts) == 3 and parts[0] == "spotify" and parts[1] == "track":
                        tid = parts[2]
                if tid:
                    all_track_ids.append(tid)

            if not items or len(all_track_ids) >= total_count:
                break
            offset += limit

        log.info("album %s (graphql): %d tracks", album_id, len(all_track_ids))
        return album_info, all_track_ids

    except Exception as exc:
        log.warning("graphql album fetch failed (%s) — falling back to REST API", exc)
        return _get_spotify_album_tracks_rest(album_id)


def _get_spotify_album_tracks_rest(album_id: str) -> tuple[dict, list]:
    """Fallback: fetch album tracks via Spotify's public REST API."""
    headers = _auth_headers()

    al_resp = requests.get(
        f"https://api.spotify.com/v1/albums/{album_id}",
        headers=headers,
        timeout=15,
    )
    al_resp.raise_for_status()
    al_data = al_resp.json()

    album_info = {
        "name":         al_data.get("name", "album"),
        "artists":      [a["name"] for a in al_data.get("artists", [])],
        "release_date": al_data.get("release_date", ""),
        "cover_url":    ((al_data.get("images") or [{}])[0]).get("url", ""),
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

    log.info("album %s (rest): %d tracks", album_id, len(track_ids))
    return album_info, track_ids


# ---------------------------------------------------------------------------
# Spotify artist support
# ---------------------------------------------------------------------------

def parse_spotify_artist_id(text: str) -> str | None:
    """Extract a 22-char Spotify artist ID from a URL, URI, or bare ID."""
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/artist/([A-Za-z0-9]{22})",
        r"spotify:artist:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def get_spotify_artist_info(artist_id: str) -> dict:
    """
    Fetch artist metadata + top 5 tracks via Spotify's public REST API.

    Returns a dict with:
      name        – str
      image_url   – str | None  (largest available image)
      top_tracks  – list[dict]  (up to 5 entries; each has id, title, artists, duration)
    """
    headers = _auth_headers()

    # Artist profile
    artist_resp = requests.get(
        f"https://api.spotify.com/v1/artists/{artist_id}",
        headers=headers,
        timeout=15,
    )
    artist_resp.raise_for_status()
    artist_data = artist_resp.json()

    name = artist_data.get("name", "Unknown Artist")
    images = artist_data.get("images") or []
    image_url: str | None = None
    if images:
        # Pick the largest image (images are sorted largest-first by Spotify)
        image_url = images[0].get("url")

    # Top tracks.
    # "market=from_token" asks Spotify to use the token's home region, which
    # avoids having to hard-code a country code.  Anonymous tokens returned by
    # Spotify's web-player auth flow sometimes lack a region claim, causing a
    # 400 error.  In that case we fall back to an explicit US market.
    top_resp = requests.get(
        f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
        headers=headers,
        params={"market": "from_token"},
        timeout=15,
    )
    if top_resp.status_code == 400:
        # Token does not carry a region claim — retry with an explicit market
        top_resp = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
            headers=headers,
            params={"market": "US"},
            timeout=15,
        )
    top_resp.raise_for_status()
    top_data = top_resp.json()

    top_tracks: list[dict] = []
    for t in (top_data.get("tracks") or [])[:5]:
        dur_ms = t.get("duration_ms") or 0
        dur_s = dur_ms // 1000
        m_val, s_val = divmod(dur_s, 60)
        top_tracks.append({
            "id":       t.get("id", ""),
            "title":    t.get("name", "Unknown"),
            "artists":  [a["name"] for a in (t.get("artists") or [])],
            "duration": f"{m_val}:{s_val:02d}",
        })

    log.info("artist %s: name=%r top_tracks=%d", artist_id, name, len(top_tracks))
    return {
        "artist_id":  artist_id,
        "name":       name,
        "image_url":  image_url,
        "top_tracks": top_tracks,
    }


def get_spotify_artist_albums(
    artist_id: str,
    group: str = "album",
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[dict], int]:
    """
    Fetch a page of artist albums or singles via Spotify's public REST API.

    Parameters
    ----------
    artist_id : Spotify artist ID
    group     : "album" or "single"
    offset    : pagination offset
    limit     : page size (max 50)

    Returns
    -------
    (items, total) where *items* is a list of dicts with keys:
      id, name, artists, release_date, total_tracks, image_url
    and *total* is the total count of matching releases.
    """
    headers = _auth_headers()
    resp = requests.get(
        f"https://api.spotify.com/v1/artists/{artist_id}/albums",
        headers=headers,
        params={
            "include_groups": group,
            "limit":          min(limit, 50),
            "offset":         offset,
            "market":         "from_token",
        },
        timeout=15,
    )
    if resp.status_code == 400:
        resp = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/albums",
            headers=headers,
            params={
                "include_groups": group,
                "limit":          min(limit, 50),
                "offset":         offset,
                "market":         "US",
            },
            timeout=15,
        )
    resp.raise_for_status()
    data = resp.json()

    total = data.get("total", 0)
    items: list[dict] = []
    for alb in (data.get("items") or []):
        images = alb.get("images") or []
        img_url = images[0].get("url") if images else None
        items.append({
            "id":           alb.get("id", ""),
            "name":         alb.get("name", "Unknown"),
            "artists":      [a["name"] for a in (alb.get("artists") or [])],
            "release_date": alb.get("release_date", ""),
            "total_tracks": alb.get("total_tracks", 0),
            "image_url":    img_url,
        })

    log.info("artist %s albums (group=%s offset=%d): %d/%d", artist_id, group, offset, len(items), total)
    return items, total


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

    # ── Tidal Alt — no credentials; shows alongside Qobuz as alternate FLAC ──
    if want_flac and info.get("track_id") and (info.get("tidal_alt_url") or info.get("tidal_alt_available")):
        choices.append({
            "label":      "\U0001f1f3\U0001f1f4 Tidal FLAC (keyless proxy)",
            "source":     "tidal_alt",
            "quality":    QUALITY_FLAC_CD,
            "audio_only": True,
            "out_ext":    "flac",
            "url":        info.get("tidal_alt_url"),
            "spotify_id": info.get("track_id"),
        })

    # ── Amazon Music — no credentials required (proxy API) ─────────────────
    if info.get("amazon_url") and want_flac:
        asin = _extract_amazon_asin(info["amazon_url"])
        if asin:
            choices.append({
                "label":      "\U0001f1fa\U0001f1f8 Amazon Music FLAC",
                "source":     "amazon",
                "quality":    QUALITY_FLAC_CD,
                "audio_only": True,
                "out_ext":    "flac",
                "url":        info["amazon_url"],
                "asin":       asin,
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

    # ── Auto — try all in priority order ───────────────────────────────────
    # Prepend as the first choice so it gets index 0 / "!1" in the menu.
    # The actual per-source choices follow and are tried in order.
    if len(choices) >= 2:
        auto_label_parts = []
        for c in choices:
            src = c.get("source", "")
            if src == "qobuz":
                lbl = c.get("label", "")
                if "Hi-Res" in lbl:
                    auto_label_parts.append("Qobuz Hi-Res")
                else:
                    auto_label_parts.append("Qobuz")
            elif src == "tidal_alt":
                auto_label_parts.append("Tidal")
            elif src == "deezer":
                auto_label_parts.append("Deezer")
            elif src == "amazon":
                auto_label_parts.append("Amazon")
            elif src == "ytmusic":
                auto_label_parts.append("YT Music")

        auto_choice = {
            "label":       "\u26a1 Auto \u2014 try all in order ({})".format(" \u2192 ".join(auto_label_parts)),
            "source":      "auto",
            "quality":     quality,
            "audio_only":  True,
            "out_ext":     "flac",
            "url":         None,
            "_sub_choices": list(choices),
        }
        choices.insert(0, auto_choice)

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

    # ── Gap 7: Check download history before downloading ───────────────────
    # Ref: SpotiFLAC backend/history.go + backend/filemanager.go ResolveOutputPathForDownload
    if source != "auto":
        track_id = info.get("track_id") or info.get("amazon_id") or info.get("tidal_id") or ""
        quality  = choice.get("quality", "")
        if track_id and quality:
            cached_fp = _check_download_history(str(track_id), source, quality)
            if cached_fp:
                log.info("download history hit: reusing %s", cached_fp)
                return cached_fp

    # ── Auto mode — try each sub-choice in order, return first that succeeds ──
    if source == "auto":
        sub_choices = choice.get("_sub_choices") or []
        if not sub_choices:
            raise RuntimeError("Auto mode has no sub-choices to try")

        last_exc: Exception | None = None
        for sub in sub_choices:
            sub_src = sub.get("source", "?")
            try:
                log.info("auto mode: trying source=%s for %r", sub_src, title)
                result = await download_track_from_choice(info, sub, download_dir, ytdlp_bin)
                log.info("auto mode: succeeded with source=%s", sub_src)
                return result
            except Exception as exc:
                log.warning(
                    "auto mode: source=%s failed (%s), trying next\u2026",
                    sub_src, exc,
                )
                last_exc = exc
                continue

        raise RuntimeError(
            "Auto mode exhausted all {} sources. Last error: {}".format(
                len(sub_choices), last_exc
            )
        )

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
        # Record to download history (Gap 7)
        _record_download_history(info.get("track_id") or qobuz_id, "qobuz", quality_tier, fp)
        return fp

    # ── Tidal Alt — no credentials; uses Spotify track ID (or Tidal ID) ─────
    if source == "tidal_alt":
        spotify_id = choice.get("spotify_id") or info.get("track_id")
        tidal_id   = info.get("tidal_id")

        tidal_result = choice.get("url") or info.get("tidal_alt_url")

        if not tidal_result and spotify_id:
            loop = asyncio.get_event_loop()
            tidal_result = await loop.run_in_executor(None, _get_tidal_alt_url, spotify_id)

        if not tidal_result and tidal_id:
            # Fallback: try the proxy with the Tidal track ID directly
            loop = asyncio.get_event_loop()
            tidal_result = await loop.run_in_executor(
                None, _get_tidal_alt_url_by_tidal_id, str(tidal_id)
            )

        if not tidal_result:
            raise RuntimeError(
                f"Tidal Alt proxy returned no URL. "
                f"spotify_id={spotify_id!r} tidal_id={tidal_id!r}"
            )

        # Gap 2: handle V2 manifest response (SpotiFLAC backend/tidal.go DownloadFromManifest)
        if isinstance(tidal_result, dict) and tidal_result.get("type") == "manifest":
            ext = _ext_from_manifest(tidal_result)
            dest_path = download_dir / f"{safe}{ext}"
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _download_tidal_manifest, tidal_result, dest_path)
        else:
            # Plain URL download
            direct_url = str(tidal_result)
            from urllib.parse import urlparse as _urlparse
            ext = Path(_urlparse(direct_url).path).suffix.lower() or ".flac"
            dest_path = download_dir / f"{safe}{ext}"
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _download_raw_stream, direct_url, dest_path)

        try:
            embed_metadata(dest_path, info)
        except Exception as exc:
            log.warning("metadata embed for tidal alt %s: %s", dest_path.name, exc)
        # Record to download history (Gap 7)
        _record_download_history(info.get("track_id") or str(tidal_id or ""), "tidal_alt", QUALITY_FLAC_CD, dest_path)
        return dest_path

    # ── Amazon Music — proxy API (no credentials) ──────────────────────────
    if source == "amazon":
        asin = choice.get("asin") or _extract_amazon_asin(choice.get("url", ""))
        if not asin:
            raise RuntimeError("Amazon ASIN not available")
        # Gap 1: thread decryption key through to _convert_or_rename_amazon
        # Ref: SpotiFLAC backend/amazon.go — apply -decryption_key before convert
        stream_url, decryption_key = _get_amazon_stream_url(asin)
        if not stream_url:
            raise RuntimeError(f"Amazon proxy returned no stream URL for ASIN {asin}")

        loop = asyncio.get_event_loop()
        raw_path = download_dir / f"{safe}.m4a.tmp"
        await loop.run_in_executor(None, _download_raw_stream, stream_url, raw_path)

        fp = await _convert_or_rename_amazon(raw_path, download_dir, safe, decryption_key)
        try:
            embed_metadata(fp, info)
        except Exception as exc:
            log.warning("metadata embed failed for %s: %s", fp.name, exc)

        # Record to download history (Gap 7)
        track_id = info.get("track_id") or asin
        _record_download_history(track_id, "amazon", "flac", fp)
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
