from __future__ import annotations

"""Tidal access-token bootstrap, refresh, and on-disk cache.

Ported from monochrome-music/monochrome:
  - functions/track/[id].js#L11-L30  (TidalAPI.getToken — client_credentials flow)
  - functions/album/[id].js#L11-L30
  - functions/artist/[id].js#L11-L30
  - functions/playlist/[id].js#L11-L30

The upstream code performs a fresh token fetch on every request; this
implementation adds an in-process + on-disk cache to reduce round-trips.
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from rubetunes.providers.monochrome.constants import (
    TIDAL_AUTH_URL,
    TIDAL_CLIENT_ID,
    TIDAL_CLIENT_SECRET,
    TOKEN_CACHE_FILENAME,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level in-process cache (access_token, expires_at)
# ---------------------------------------------------------------------------
_token_lock = asyncio.Lock()
_cached_token: str | None = None
_token_expires_at: float = 0.0          # epoch seconds


def _cache_path() -> Path:
    """Return path to the on-disk token cache file."""
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    cache_dir = base / "rubetunes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / TOKEN_CACHE_FILENAME


def _load_cached_token() -> tuple[str, float] | None:
    """Load token + expiry from disk; returns None if missing/expired."""
    try:
        data: dict[str, Any] = json.loads(_cache_path().read_text())
        token: str = data["access_token"]
        expires_at: float = float(data["expires_at"])
        if time.time() < expires_at - 60:   # 60-second safety margin
            return token, expires_at
    except Exception:
        pass
    return None


def _save_cached_token(access_token: str, expires_in: int) -> None:
    """Persist token + expiry to disk."""
    try:
        expires_at = time.time() + expires_in
        _cache_path().write_text(
            json.dumps({"access_token": access_token, "expires_at": expires_at})
        )
    except Exception as exc:
        log.debug("Could not write token cache: %s", exc)


async def _fetch_new_token(client: httpx.AsyncClient) -> tuple[str, int]:
    """Request a fresh client-credentials token from auth.tidal.com.

    Source: functions/track/[id].js#L11-L30 (TidalAPI.getToken)
    POST https://auth.tidal.com/v1/oauth2/token
    Headers: Authorization: Basic <base64(client_id:client_secret)>
             Content-Type: application/x-www-form-urlencoded
    Body:    client_id=...&client_secret=...&grant_type=client_credentials
    Response fields consumed: access_token, expires_in
    """
    credentials = base64.b64encode(
        f"{TIDAL_CLIENT_ID}:{TIDAL_CLIENT_SECRET}".encode()
    ).decode()

    resp = await client.post(
        TIDAL_AUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "client_id": TIDAL_CLIENT_ID,
            "client_secret": TIDAL_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], int(data.get("expires_in", 3600))


async def get_token(client: httpx.AsyncClient | None = None) -> str:
    """Return a valid Tidal access token, refreshing if necessary.

    Uses double-checked locking so concurrent callers wait for a single
    in-flight refresh rather than each making their own.
    """
    global _cached_token, _token_expires_at

    # Fast path: check in-process cache first (no lock)
    if _cached_token and time.time() < _token_expires_at - 60:
        return _cached_token

    async with _token_lock:
        # Re-check after acquiring lock (another coroutine may have refreshed)
        if _cached_token and time.time() < _token_expires_at - 60:
            return _cached_token

        # Try on-disk cache
        cached = _load_cached_token()
        if cached:
            _cached_token, _token_expires_at = cached
            return _cached_token

        # Fetch a new token
        _own_client = client is None
        if _own_client:
            client = httpx.AsyncClient()
        try:
            access_token, expires_in = await _fetch_new_token(client)
            _cached_token = access_token
            _token_expires_at = time.time() + expires_in
            _save_cached_token(access_token, expires_in)
            return _cached_token
        finally:
            if _own_client:
                await client.aclose()


def clear_token_cache() -> None:
    """Evict in-process token cache (useful in tests)."""
    global _cached_token, _token_expires_at
    _cached_token = None
    _token_expires_at = 0.0
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:
        pass
