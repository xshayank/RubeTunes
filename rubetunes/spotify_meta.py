from __future__ import annotations

"""Spotify metadata: TOTP, tokens, GraphQL, parse functions, ISRC helpers."""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import requests

from rubetunes.cache import _get_cached_isrc, _put_cached_isrc

log = logging.getLogger("spotify_dl")

__all__ = [
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "DEEZER_ARL",
    "QOBUZ_EMAIL",
    "QOBUZ_PASSWORD",
    "_SPOTIFY_TOTP_SECRET",
    "_SPOTIFY_TOTP_VERSION",
    "_SPOTIFY_CLIENT_VERSION_FALLBACK",
    "_BASE62",
    "_b62_to_int",
    "track_id_to_gid",
    "parse_spotify_track_id",
    "parse_tidal_track_id",
    "parse_qobuz_track_id",
    "parse_amazon_track_id",
    "_totp",
    "_get_totp_secret",
    "_token_cache",
    "_spotify_token_cache_path",
    "_load_spotify_token",
    "_save_spotify_token",
    "_HEADERS_BASE",
    "_anon_session",
    "_anon_session_lock",
    "_ensure_anon_session",
    "_reset_anon_session",
    "_fetch_spotify_server_time",
    "_fetch_anon_token",
    "_fetch_cc_token",
    "get_token",
    "_auth_headers",
    "_spclient_file_id_to_hex",
    "_fetch_internal_meta",
    "_fetch_public_meta",
    "_parse_internal",
    "_parse_public",
    "_SPOTIFY_GRAPHQL_ENDPOINT",
    "_GRAPHQL_HASH_GET_TRACK",
    "_GRAPHQL_HASH_GET_ALBUM",
    "_GRAPHQL_HASH_FETCH_PLAYLIST",
    "_spotify_graphql_query",
    "_fetch_track_graphql",
    "_parse_graphql_track",
    "_fetch_album_graphql_page",
    "_fetch_playlist_graphql_page",
    "SpotifyClient",
    "_sp_str",
    "_sp_map",
    "_sp_list",
    "_sp_float",
    "_sp_extract_artists",
    "_sp_extract_cover",
    "_sp_extract_duration",
    "filter_track",
    "filter_album",
    "filter_playlist",
    "_ISRC_CACHE_FILE",
    "_isrc_cache_lock",
    "_isrc_cache_path",
    "_get_cached_isrc",
    "_put_cached_isrc",
    "_ISRC_RE",
    "_isrc_soundplate",
    "get_token",
    "get_lyrics",
    "spotify_search",
    "_fetch_lyrics_lrclib",
    "_LRCLIB_BASE",
    "_LRCLIB_UA",
    "parse_spotify_playlist_id",
    "parse_spotify_album_id",
    "parse_spotify_artist_id",
    "get_spotify_playlist_tracks",
    "get_spotify_album_tracks",
    "get_spotify_artist_info",
    "get_spotify_artist_albums",
]

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEEZER_ARL            = os.getenv("DEEZER_ARL",            "").strip()
QOBUZ_EMAIL           = os.getenv("QOBUZ_EMAIL",           "").strip()
QOBUZ_PASSWORD        = os.getenv("QOBUZ_PASSWORD",        "").strip()

# Hardcoded fallback TOTP secret.
# SECURITY NOTE: This is *not* a user credential or private key.  It is the
# public Spotify web-player TOTP secret, embedded in Spotify's own client-side
# JavaScript bundle and widely documented in open-source projects (SpotiFLAC,
# librespot, etc.).  It allows anonymous read-only access identical to opening
# open.spotify.com in a browser.  Spotify rotates this value periodically;
# set SPOTIFY_TOTP_SECRET env var to override without a code change.
_SPOTIFY_TOTP_SECRET  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
_SPOTIFY_TOTP_VERSION = 61
_SPOTIFY_CLIENT_VERSION_FALLBACK = "1.2.52.442.g55a7e7d3"

_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

# ---------------------------------------------------------------------------
# TOTP secret resolution — env var → scraped → hardcoded fallback
# ---------------------------------------------------------------------------
_scraped_totp_secret:    str   = ""
_scraped_totp_secret_at: float = 0.0
_TOTP_SCRAPE_TTL = 86_400.0  # 24 h

# Bundle URL patterns used to find the Spotify web-player JS
_SP_BUNDLE_RE = re.compile(
    r'src=["\']([^"\']+/web-player/[^"\']+\.js)["\']'
    r'|"(https://[^"]+spotifycdn\.com/cdn/build/[^"]+\.js)"'
)
# Pattern inside the bundle JS for the base-32 TOTP secret
_SP_TOTP_SECRET_RE = re.compile(
    r'(?:totpSecret|totp_secret)["\']?\s*[=:]\s*["\']([A-Z2-7]{30,})["\']'
    r'|["\']([A-Z2-7]{60,})["\']'
)


def _get_totp_secret() -> str:
    """Return the active TOTP secret: env var > scraped > hardcoded fallback."""
    env_secret = os.getenv("SPOTIFY_TOTP_SECRET", "").strip()
    if env_secret:
        return env_secret
    if _scraped_totp_secret and (time.time() - _scraped_totp_secret_at) < _TOTP_SCRAPE_TTL:
        return _scraped_totp_secret
    return _SPOTIFY_TOTP_SECRET


def _try_scrape_totp_secret(session: "requests.Session") -> str | None:
    """Attempt to extract the TOTP secret from Spotify's web-player JS bundle.

    Best-effort — returns None on any failure; never raises.
    """
    global _scraped_totp_secret, _scraped_totp_secret_at
    try:
        resp = session.get("https://open.spotify.com", timeout=20)
        if not resp.ok:
            return None
        bundle_urls: list[str] = []
        for m in _SP_BUNDLE_RE.finditer(resp.text):
            url = m.group(1) or m.group(2) or ""
            if url:
                if url.startswith("/"):
                    url = "https://open.spotify.com" + url
                bundle_urls.append(url)

        for bundle_url in bundle_urls[:5]:
            try:
                br = session.get(bundle_url, timeout=30)
                if not br.ok:
                    continue
                sm = _SP_TOTP_SECRET_RE.search(br.text)
                if sm:
                    candidate = sm.group(1) or sm.group(2) or ""
                    if len(candidate) >= 30:
                        _scraped_totp_secret    = candidate
                        _scraped_totp_secret_at = time.time()
                        log.debug("scraped Spotify TOTP secret from bundle (%d chars)", len(candidate))
                        return candidate
            except Exception:
                continue
    except Exception as exc:
        log.debug("totp secret scrape failed: %s", exc)
    return None


def _b62_to_int(s: str) -> int:
    n = 0
    for c in s:
        n = n * 62 + _BASE62.index(c)
    return n


def track_id_to_gid(track_id: str) -> str:
    return hex(_b62_to_int(track_id))[2:].zfill(32)


def parse_spotify_track_id(text: str) -> str | None:
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
    text = text.strip()
    for pattern in (
        r"open\.qobuz\.com/track/(\d+)",
        r"qobuz\.com/[a-z\-]+/album/[^/]+/[^/]+/track/(\d+)",
        r"qobuz\.com/[a-z\-]+/track/[^/]+/(\d+)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    if re.fullmatch(r"\d{5,12}", text):
        return text
    return None


def parse_amazon_track_id(text: str) -> str | None:
    text = text.strip()
    for pattern in (
        r"music\.amazon\.[a-z.]+/tracks/([A-Z0-9]{10,})",
        r"[?&]trackAsin=([A-Z0-9]{10,})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def _totp(secret_b32: str, server_time: int | None = None) -> str:
    """Compute a 6-digit TOTP code from a base-32 secret.

    *server_time* (Unix seconds) overrides the local clock when provided,
    so clock skew between the bot host and Spotify servers doesn't break auth.
    """
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    t = server_time if server_time is not None else int(time.time())
    counter = t // 30
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset: offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


# ---------------------------------------------------------------------------
# Persistent anonymous session — required so Spotify's /api/token receives
# the sp_t cookie that is set by visiting open.spotify.com first.
# Without sp_t the TOTP challenge always fails with HTTP 4xx / empty token.
# ---------------------------------------------------------------------------
_anon_session: requests.Session | None = None
_anon_session_lock = threading.Lock()
_anon_session_created_at: float = 0.0
_ANON_SESSION_TTL = 3600.0  # recreate the session every hour

_SP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _reset_anon_session() -> None:
    """Discard the current anonymous session so it will be recreated next call."""
    global _anon_session, _anon_session_created_at
    with _anon_session_lock:
        _anon_session = None
        _anon_session_created_at = 0.0


def _ensure_anon_session(*, force_refresh: bool = False) -> "requests.Session":
    """Return (and lazily create) the cookie-bearing anonymous Spotify session.

    The session visits open.spotify.com on first creation so the sp_t cookie
    is populated before any /api/token requests are made.
    """
    global _anon_session, _anon_session_created_at
    with _anon_session_lock:
        now = time.time()
        if (
            force_refresh
            or _anon_session is None
            or (now - _anon_session_created_at) > _ANON_SESSION_TTL
        ):
            s = requests.Session()
            s.headers.update({
                "User-Agent":      _SP_UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
            try:
                r = s.get("https://open.spotify.com", timeout=20, allow_redirects=True)
                sp_t = s.cookies.get("sp_t", "(none)")
                log.debug("anon session init: status=%d sp_t=%s", r.status_code, sp_t)
            except Exception as exc:
                log.debug("anon session init error (continuing): %s", exc)
            _anon_session = s
            _anon_session_created_at = now
        return _anon_session


def _fetch_spotify_server_time(session: "requests.Session") -> int | None:
    """Fetch Spotify's server-side Unix timestamp for TOTP clock-sync.

    Returns the server time in seconds, or None if the endpoint is unavailable.
    """
    for url in (
        "https://open.spotify.com/api/server-time",
        "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
    ):
        try:
            resp = session.get(url, timeout=10)
            if resp.ok:
                data = resp.json()
                t = data.get("serverTime") or data.get("server_time")
                if t:
                    return int(t)
        except Exception:
            pass
    return None


_token_cache: dict = {}


def _spotify_token_cache_path() -> Path:
    cache_dir = Path(tempfile.gettempdir()) / "tele2rub"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "spotify-anon-token.json"


def _load_spotify_token() -> dict:
    try:
        data = json.loads(_spotify_token_cache_path().read_text())
        if data.get("token") and data.get("expires_at"):
            return data
    except Exception:
        pass
    return {}


def _save_spotify_token(token: str, expires_at: float) -> None:
    try:
        _spotify_token_cache_path().write_text(
            json.dumps({"token": token, "expires_at": expires_at})
        )
    except Exception as exc:
        log.debug("could not save spotify token cache: %s", exc)


_HEADERS_BASE = {
    "User-Agent": _SP_UA,
    "Accept": "application/json",
    "Accept-Language": "en",
    "Referer": "https://open.spotify.com/",
    "Origin": "https://open.spotify.com",
}


def _fetch_anon_token() -> tuple[str, float]:
    """Obtain a Spotify anonymous access token via the TOTP web-player flow.

    Uses a persistent session (with sp_t cookie) and syncs the TOTP counter
    to Spotify's server time to handle host clock skew.
    """
    sess = _ensure_anon_session()
    server_time = _fetch_spotify_server_time(sess)
    secret      = _get_totp_secret()
    totp_code   = _totp(secret, server_time)
    params: dict = {
        "reason":      "init",
        "productType": "web-player",
        "totp":        totp_code,
        "totpVer":     str(_SPOTIFY_TOTP_VERSION),
        "totpServer":  totp_code,
    }
    resp = sess.get(
        "https://open.spotify.com/api/token",
        params=params,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("accessToken", "")
    if not token:
        raise RuntimeError(
            f"Spotify /api/token returned no accessToken (isAnonymous={data.get('isAnonymous')!r})"
        )
    expires = (data.get("accessTokenExpirationTimestampMs") or 0) / 1000
    return token, expires or time.time() + 3600


def _fetch_cc_token() -> tuple[str, float]:
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
    """Return a valid Spotify access token, refreshing as needed.

    Priority:
    1. Unexpired in-memory cache.
    2. Unexpired on-disk cache.
    3. Fresh anon token via TOTP (with retry + session reset).
    4. Scraped TOTP secret (if hardcoded secret stops working).
    5. Client-credentials token (if SPOTIFY_CLIENT_ID/SECRET are set).
    """
    now = time.time()

    # 1 — in-memory
    if _token_cache.get("expires_at", 0) > now + 30:
        return _token_cache["token"]

    # 2 — disk cache
    if not _token_cache:
        disk = _load_spotify_token()
        if disk.get("expires_at", 0) > now + 30:
            _token_cache.update(disk)
            return _token_cache["token"]

    # 3 — fresh anon token with up to 2 attempts (second attempt resets session)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            if attempt == 1:
                # Force a fresh session — the previous sp_t may have expired
                _reset_anon_session()
            token, expires = _fetch_anon_token()
            _token_cache.update({"token": token, "expires_at": expires})
            _save_spotify_token(token, expires)
            return token
        except Exception as exc:
            log.warning("anon token attempt %d failed: %s", attempt + 1, exc)
            last_exc = exc

    # 4 — try scraping a fresh TOTP secret from the bundle then retry once more
    try:
        sess = _ensure_anon_session()
        scraped = _try_scrape_totp_secret(sess)
        if scraped and scraped != _SPOTIFY_TOTP_SECRET:
            log.info("retrying anon token with scraped TOTP secret")
            token, expires = _fetch_anon_token()
            _token_cache.update({"token": token, "expires_at": expires})
            _save_spotify_token(token, expires)
            return token
    except Exception as exc:
        log.warning("anon token (scraped secret) failed: %s", exc)

    # 5 — client credentials fallback
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            token, expires = _fetch_cc_token()
            _token_cache.update({"token": token, "expires_at": expires})
            return token
        except Exception as exc:
            log.error("CC token also failed: %s", exc)

    raise RuntimeError(f"Cannot get Spotify token. Last error: {last_exc}")


def _auth_headers() -> dict:
    return {**_HEADERS_BASE, "Authorization": f"Bearer {get_token()}"}


def _spclient_file_id_to_hex(fid: str) -> str:
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
    gid = track_id_to_gid(track_id)
    url = f"https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_public_meta(track_id: str) -> dict:
    url = f"https://api.spotify.com/v1/tracks/{track_id}"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_internal(meta: dict) -> dict:
    name = meta.get("name", "")
    artists = [a.get("name", "") for a in meta.get("artist", [])]
    album = meta.get("album", {})
    album_name = album.get("name", "")
    cover_url = ""
    images = album.get("cover_group", {}).get("image", [])
    if images:
        best = max(images, key=lambda x: x.get("width", 0))
        fid = best.get("file_id", "")
        if fid:
            hex_fid = _spclient_file_id_to_hex(fid)
            cover_url = f"https://i.scdn.co/image/{hex_fid}" if hex_fid else ""
    date = album.get("date", {})
    if isinstance(date, dict):
        y = str(date.get("year", ""))
        mo = date.get("month")
        d = date.get("day")
        release_date = f"{y}-{int(mo):02d}-{int(d):02d}" if mo and d else y
    else:
        release_date = str(date)
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


_SPOTIFY_GRAPHQL_ENDPOINT = "https://api-partner.spotify.com/pathfinder/v1/query"
_GRAPHQL_HASH_GET_TRACK       = "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294"
_GRAPHQL_HASH_GET_ALBUM       = "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10"
_GRAPHQL_HASH_FETCH_PLAYLIST  = "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"


def _spotify_graphql_query(payload: dict) -> dict:
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
    track = ((data.get("data") or {}).get("trackUnion") or {})
    artist_items = ((track.get("artists") or {}).get("items") or [])
    artists = [
        (item.get("profile") or {}).get("name", "")
        for item in artist_items
        if (item.get("profile") or {}).get("name")
    ]
    album = (track.get("albumOfTrack") or {})
    release_date = ""
    date_obj = (album.get("date") or {})
    iso = date_obj.get("isoString", "")
    if iso:
        release_date = iso[:10]
    elif date_obj.get("year"):
        release_date = str(date_obj["year"])
    cover_url = ""
    sources = ((album.get("coverArt") or {}).get("sources") or [])
    if sources:
        cover_url = sources[0].get("url", "")
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


class SpotifyClient:
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

    def _get_session_info(self) -> None:
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
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_access_token(self) -> None:
        server_time = _fetch_spotify_server_time(self._session)
        totp_code   = _totp(_get_totp_secret(), server_time)
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
            raise RuntimeError(f"spotify access token request failed: HTTP {resp.status_code}")
        data = resp.json()
        self._access_token = data.get("accessToken", "")
        if not self._access_token:
            raise RuntimeError(
                f"spotify access token response missing accessToken (isAnonymous={data.get('isAnonymous')!r})"
            )
        self._client_id = data.get("clientId", "")
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_client_token(self) -> None:
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
            headers={"Authority": "clienttoken.spotify.com", "Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"spotify client token request failed: HTTP {resp.status_code}")
        data = resp.json()
        if data.get("response_type") != "RESPONSE_GRANTED_TOKEN_RESPONSE":
            raise RuntimeError(f"invalid client token response type: {data.get('response_type')!r}")
        self._client_token = (data.get("granted_token") or {}).get("token", "")

    def initialize(self) -> None:
        self._get_session_info()
        self._get_access_token()
        self._get_client_token()

    def query(self, payload: dict) -> dict:
        if not self._access_token or not self._client_token:
            self.initialize()
        resp = self._session.post(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            json=payload,
            headers={
                "Authorization":       f"Bearer {self._access_token}",
                "Client-Token":        self._client_token,
                "Spotify-App-Version": self._client_version,
                "Content-Type":        "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"spotify API query failed: HTTP {resp.status_code}")
        return resp.json()


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
                        for prefix in ("ab67616d0000b273", "ab67616d00001e02", "ab67616d00004851"):
                            if prefix in img_part:
                                image_id = img_part.split(prefix)[-1]
                                break
    large_url = f"https://i.scdn.co/image/ab67616d000082c1{image_id}" if image_id else ""
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


def filter_track(data: dict, separator: str = ", ", album_fetch_data: dict | None = None) -> dict:
    track_data = _sp_map(_sp_map(data, "data"), "trackUnion")
    if not track_data:
        return {}
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
        artists = _sp_extract_artists(_sp_map(_sp_map(track_data, "albumOfTrack"), "artists"))
    artists_str = separator.join(a["name"] for a in artists)
    album_data = _sp_map(track_data, "albumOfTrack")
    album_info: dict | None = None
    copyright_texts: list[str] = []
    disc_info_total: int | None = None
    if album_data:
        for item in _sp_list(_sp_map(album_data, "copyright"), "items"):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "P":
                t = _sp_str(item, "text")
                if t:
                    copyright_texts.append(t)
        disc_numbers: set[int] = set()
        for item in _sp_list(_sp_map(album_data, "tracks"), "items"):
            if not isinstance(item, dict):
                continue
            d = int(_sp_float(_sp_map(item, "track"), "discNumber")) or 1
            disc_numbers.add(d)
        if disc_numbers:
            disc_info_total = max(disc_numbers)
        date_info = _sp_map(album_data, "date")
        iso = _sp_str(date_info, "isoString")
        if iso:
            release_date = iso[:10]
            release_year: int | None = int(iso[:4]) if len(iso) >= 4 else None
        else:
            y  = _sp_str(date_info, "year")
            mo = _sp_str(date_info, "month")
            dy = _sp_str(date_info, "day")
            if y:
                release_year = int(y)
                release_date = f"{y}-{int(mo):02d}-{int(dy):02d}" if mo and dy else y
            else:
                release_date = ""
                release_year = None
        tracks_data  = _sp_map(album_data, "tracks")
        tracks_count = int(_sp_float(tracks_data, "totalCount"))
        album_uri = _sp_str(album_data, "uri")
        album_id  = _sp_str(album_data, "id") or (album_uri.split(":")[-1] if ":" in album_uri else "")
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
    cover = _sp_extract_cover(_sp_map(track_data, "visualIdentity"))
    if cover is None and album_data:
        cover = _sp_extract_cover(_sp_map(album_data, "coverArt"))
    duration_ms  = _sp_float(_sp_map(track_data, "duration"), "totalMilliseconds")
    duration_str = _sp_extract_duration(duration_ms)
    disc_number = int(_sp_float(track_data, "discNumber")) or 1
    max_disc_from_album   = 0
    total_discs_from_album = 0
    if album_fetch_data:
        album_union = _sp_map(_sp_map(album_fetch_data, "data"), "albumUnion")
        if album_union:
            total_discs_from_album = int(_sp_float(_sp_map(album_union, "discs"), "totalCount"))
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
        artists_data      = _sp_map(track, "artists")
        track_artists     = _sp_extract_artists(artists_data)
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
        owner_info = {"name": _sp_str(owner_data, "name"), "avatar": avatar_url}
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
        track_id  = _sp_str(track_data, "id") or (track_uri.split(":")[-1] if ":" in track_uri else "")
        album_data       = _sp_map(track_data, "albumOfTrack")
        album_name       = album_id = album_artists_str = ""
        track_cover: str | None = None
        if album_data:
            album_name = _sp_str(album_data, "name")
            album_uri  = _sp_str(album_data, "uri")
            album_id   = album_uri.split(":")[-1] if ":" in album_uri else ""
            cover_obj  = _sp_extract_cover(_sp_map(album_data, "coverArt"))
            if cover_obj:
                track_cover = cover_obj.get("small") or cover_obj.get("medium") or cover_obj.get("large")
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


from rubetunes.cache import (  # noqa: E402  (re-import to expose in this namespace)
    _ISRC_CACHE_FILE,
    _isrc_cache_lock,
    _isrc_cache_path,
)

_ISRC_RE = re.compile(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}')


def _isrc_soundplate(track_id: str) -> str | None:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://phpstack-822472-6184058.cloudwaysapps.com/?",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
        resp = requests.get(
            "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php",
            params={"q": f"https://open.spotify.com/track/{track_id}"},
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            body = resp.text
            try:
                data = resp.json()
                isrc = data.get("isrc") or (data.get("data") or {}).get("isrc") or ""
                if isrc:
                    m = _ISRC_RE.search(isrc.upper())
                    if m:
                        return m.group(0)
            except Exception:
                pass
            m = _ISRC_RE.search(body.upper())
            if m:
                return m.group(0)
    except Exception as exc:
        log.warning("soundplate fallback: %s", exc)
    return None


_LRCLIB_BASE = "https://lrclib.net/api"
_LRCLIB_UA   = "Tele2Rub/1.0 (https://github.com/xshayank/Tele2Rub)"


def _fetch_lyrics_lrclib(track: str, artist: str, album: str = "", duration: int = 0) -> dict | None:
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
                        return {"synced_lyrics": item["syncedLyrics"], "plain_lyrics": item.get("plainLyrics", ""), "is_synced": True}
                item = results[0]
                return {"synced_lyrics": "", "plain_lyrics": item.get("plainLyrics", ""), "is_synced": False}
    except Exception as exc:
        log.debug("lrclib search: %s", exc)
    return None


def get_lyrics(track_name: str, artist_name: str, album_name: str = "", duration: int = 0) -> str | None:
    result = _fetch_lyrics_lrclib(track_name, artist_name, album_name, duration)
    if not result:
        return None
    if result["is_synced"] and result["synced_lyrics"]:
        return result["synced_lyrics"]
    if result["plain_lyrics"]:
        return result["plain_lyrics"]
    return None


def parse_spotify_playlist_id(text: str) -> str | None:
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
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/album/([A-Za-z0-9]{22})",
        r"spotify:album:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def parse_spotify_artist_id(text: str) -> str | None:
    text = text.strip()
    for pattern in (
        r"open\.spotify\.com/artist/([A-Za-z0-9]{22})",
        r"spotify:artist:([A-Za-z0-9]{22})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def get_spotify_playlist_tracks(playlist_id: str) -> tuple[dict, list[str]]:
    """Return (playlist_info, track_ids) for a Spotify playlist.

    Handles pagination (Spotify returns up to 100 items per page).
    Skips local/None tracks.
    """
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Spotify playlist API error: HTTP {resp.status_code}")
    data = resp.json()

    images = data.get("images") or []
    playlist_info = {
        "name": data.get("name", ""),
        "owner": (data.get("owner") or {}).get("display_name", ""),
        "total_tracks": (data.get("tracks") or {}).get("total", 0),
        "image_url": images[0].get("url", "") if images else "",
    }

    track_ids: list[str] = []
    tracks_page = data.get("tracks") or {}
    while True:
        for item in tracks_page.get("items") or []:
            track = (item or {}).get("track")
            if not track:
                continue
            tid = track.get("id")
            if tid:
                track_ids.append(tid)
        next_url = tracks_page.get("next")
        if not next_url:
            break
        resp = requests.get(next_url, headers=_auth_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Spotify playlist tracks API error: HTTP {resp.status_code}")
        tracks_page = resp.json()

    return playlist_info, track_ids


def get_spotify_album_tracks(album_id: str) -> tuple[dict, list[str]]:
    """Return (album_info, track_ids) for a Spotify album.

    Handles pagination if total_tracks > 50.
    """
    url = f"https://api.spotify.com/v1/albums/{album_id}"
    resp = requests.get(url, headers=_auth_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Spotify album API error: HTTP {resp.status_code}")
    data = resp.json()

    images = data.get("images") or []
    album_info = {
        "name": data.get("name", ""),
        "artists": [a["name"] for a in (data.get("artists") or []) if a.get("name")],
        "release_date": data.get("release_date", ""),
        "total_tracks": data.get("total_tracks", 0),
        "image_url": images[0].get("url", "") if images else "",
    }

    track_ids: list[str] = []
    tracks_page = data.get("tracks") or {}
    while True:
        for item in tracks_page.get("items") or []:
            if not item:
                continue
            tid = item.get("id")
            if tid:
                track_ids.append(tid)
        next_url = tracks_page.get("next")
        if not next_url:
            break
        resp = requests.get(next_url, headers=_auth_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Spotify album tracks API error: HTTP {resp.status_code}")
        tracks_page = resp.json()

    return album_info, track_ids


def get_spotify_artist_info(artist_id: str) -> dict:
    """Return artist metadata including top-5 tracks.

    Top tracks are fetched via /v1/artists/{id}/top-tracks?market=US.
    """
    artist_url = f"https://api.spotify.com/v1/artists/{artist_id}"
    resp = requests.get(artist_url, headers=_auth_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Spotify artist API error: HTTP {resp.status_code}")
    artist_data = resp.json()

    images = artist_data.get("images") or []
    image_url = images[0].get("url", "") if images else ""

    top_url = f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks"
    resp = requests.get(top_url, params={"market": "US"}, headers=_auth_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Spotify artist top-tracks API error: HTTP {resp.status_code}")
    top_data = resp.json()

    top_tracks = []
    for track in (top_data.get("tracks") or [])[:5]:
        ms = track.get("duration_ms", 0)
        secs = ms // 1000
        minutes, seconds = divmod(secs, 60)
        top_tracks.append({
            "id": track.get("id", ""),
            "title": track.get("name", ""),
            "artists": [a["name"] for a in (track.get("artists") or []) if a.get("name")],
            "duration": f"{minutes}:{seconds:02d}",
        })

    return {
        "name": artist_data.get("name", ""),
        "image_url": image_url,
        "top_tracks": top_tracks,
    }


def get_spotify_artist_albums(
    artist_id: str, group: str, offset: int, limit: int
) -> tuple[list[dict], int]:
    """Return (items, total) for an artist's albums or singles.

    group must be "album" or "single". offset and limit are passed straight
    through to the Spotify API.
    """
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    params = {
        "include_groups": group,
        "market": "US",
        "limit": limit,
        "offset": offset,
    }
    resp = requests.get(url, params=params, headers=_auth_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Spotify artist albums API error: HTTP {resp.status_code}")
    data = resp.json()

    total = data.get("total", 0)
    items = []
    for alb in data.get("items") or []:
        if not alb:
            continue
        images = alb.get("images") or []
        items.append({
            "id": alb.get("id", ""),
            "name": alb.get("name", ""),
            "artists": [a["name"] for a in (alb.get("artists") or []) if a.get("name")],
            "release_date": alb.get("release_date", ""),
            "total_tracks": alb.get("total_tracks", 0),
            "image_url": images[0].get("url", "") if images else "",
        })

    return items, total


def spotify_search(query: str, limit: int = 10) -> list[dict]:
    """Search Spotify for tracks matching *query*.

    Returns up to *limit* track info dicts with keys:
    track_id, title, artists, album, duration, url.
    Uses the public search API with the anonymous bearer token.
    """
    try:
        token = get_token()
    except Exception as exc:
        log.warning("spotify_search: could not get token: %s", exc)
        return []

    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": min(limit, 50)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("spotify_search: API call failed: %s", exc)
        return []

    results = []
    for item in (data.get("tracks") or {}).get("items") or []:
        if not item:
            continue
        track_id = item.get("id", "")
        artists = [a["name"] for a in (item.get("artists") or []) if a.get("name")]
        ms = item.get("duration_ms", 0)
        secs = ms // 1000
        minutes, seconds = divmod(secs, 60)
        duration = f"{minutes}:{seconds:02d}"
        results.append({
            "track_id": track_id,
            "title": item.get("name", "Unknown"),
            "artists": artists,
            "album": (item.get("album") or {}).get("name", ""),
            "duration": duration,
            "url": f"https://open.spotify.com/track/{track_id}",
        })
    return results
