"""Session bootstrap for the Spotify web-player auth flow.

Ported from spotbye/SpotiFLAC (``backend/spotfetch.go`` — SpotifyClient.*).

Auth chain (in order):
  1. Scrape ``https://open.spotify.com`` → extract ``clientVersion`` from the
     ``<script id="appServerConfig">`` tag.
  2. Call ``https://open.spotify.com/api/token`` with TOTP params
     (``totp``, ``totpVer``, ``ts``, ``cTime``) → anonymous ``accessToken``.
  3. Fallback: ``https://accounts.spotify.com/api/token`` with
     ``SPOTIFY_CLIENT_ID`` / ``SPOTIFY_CLIENT_SECRET`` env vars.
  4. Call ``https://clienttoken.spotify.com/v1/clienttoken`` →
     ``client-token`` header (required by pathfinder/v2 GraphQL).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

import requests

from rubetunes.spotify.totp import generate_totp, SPOTIFY_TOTP_VERSION

log = logging.getLogger("spotify_dl")

_CLIENT_VERSION_FALLBACK = "1.2.52.442.g55a7e7d3"

_SP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def get_session_client_version(session: requests.Session) -> str:
    """Scrape ``https://open.spotify.com`` and extract ``clientVersion``.

    Returns the version string, or :data:`_CLIENT_VERSION_FALLBACK` if the
    page cannot be fetched or the ``appServerConfig`` script tag is absent.
    """
    try:
        resp = session.get("https://open.spotify.com", timeout=30)
        resp.raise_for_status()
        m = re.search(
            r'<script id="appServerConfig" type="text/plain">([^<]+)</script>',
            resp.text,
        )
        if m:
            cfg = json.loads(base64.b64decode(m.group(1)).decode())
            version = cfg.get("clientVersion", "")
            if version:
                return version
    except Exception as exc:
        log.debug("get_session_client_version: %s", exc)
    return _CLIENT_VERSION_FALLBACK


def _get_server_time(session: requests.Session) -> int | None:
    """Return Spotify's server Unix timestamp for TOTP clock-sync, or None."""
    try:
        resp = session.get("https://open.spotify.com/api/server-time", timeout=10)
        if resp.ok:
            data = resp.json()
            t = data.get("serverTime") or data.get("server_time")
            if t:
                return int(t)
    except Exception:
        pass
    return None


def get_anon_token(session: requests.Session, client_version: str = "") -> tuple[str, str]:
    """Fetch an anonymous Spotify access token via the TOTP web-player flow.

    Returns ``(access_token, client_id)``.  Raises ``RuntimeError`` on failure.

    The session must have visited ``https://open.spotify.com`` first so that
    the ``sp_t`` cookie is populated.
    """
    server_time = _get_server_time(session)
    totp_code   = generate_totp(ts=server_time)
    resp = session.get(
        "https://open.spotify.com/api/token",
        params={
            "reason":      "init",
            "productType": "web-player",
            "totp":        totp_code,
            "totpVer":     str(SPOTIFY_TOTP_VERSION),
            "totpServer":  totp_code,
        },
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Spotify /api/token request failed: HTTP {resp.status_code}"
        )
    data         = resp.json()
    access_token = data.get("accessToken", "")
    if not access_token:
        raise RuntimeError(
            f"Spotify /api/token returned no accessToken "
            f"(isAnonymous={data.get('isAnonymous')!r})"
        )
    return access_token, data.get("clientId", "")


def get_cc_token() -> tuple[str, float]:
    """Fetch a client-credentials Spotify token.

    Requires ``SPOTIFY_CLIENT_ID`` and ``SPOTIFY_CLIENT_SECRET`` env vars.
    Returns ``(access_token, expires_at_unix)``.
    """
    client_id     = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set for CC token"
        )
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], time.time() + data.get("expires_in", 3600)


def get_client_token(
    session: requests.Session,
    client_id: str,
    device_id: str,
    client_version: str,
) -> str:
    """Fetch the ``client-token`` header required by pathfinder/v2 GraphQL.

    Returns the token string.  Raises ``RuntimeError`` on failure.
    """
    payload = {
        "client_data": {
            "client_version": client_version,
            "client_id":      client_id,
            "js_sdk_data": {
                "device_brand": "unknown",
                "device_model": "unknown",
                "os":           "windows",
                "os_version":   "NT 10.0",
                "device_id":    device_id,
                "device_type":  "computer",
            },
        }
    }
    resp = session.post(
        "https://clienttoken.spotify.com/v1/clienttoken",
        json=payload,
        headers={
            "Authority":     "clienttoken.spotify.com",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Spotify clienttoken request failed: HTTP {resp.status_code}"
        )
    data = resp.json()
    if data.get("response_type") != "RESPONSE_GRANTED_TOKEN_RESPONSE":
        raise RuntimeError(
            f"invalid client token response_type: {data.get('response_type')!r}"
        )
    token = (data.get("granted_token") or {}).get("token", "")
    if not token:
        raise RuntimeError("Spotify clienttoken response missing token field")
    return token
