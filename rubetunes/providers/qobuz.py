from __future__ import annotations

"""Qobuz API credentials, resolution, and stream helpers."""

import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "_QOBUZ_API_BASE",
    "_QOBUZ_DEFAULT_APP_ID",
    "_QOBUZ_DEFAULT_APP_SECRET",
    "_QOBUZ_OPEN_PROBE_URL",
    "_QOBUZ_CREDS_CACHE_TTL",
    "_QOBUZ_PROBE_ISRC",
    "_QOBUZ_UA",
    "_qobuz_bundle_re",
    "_qobuz_app_id_re",
    "_qobuz_app_secret_re",
    "_qobuz_creds_lock",
    "_qobuz_creds_cache",
    "_qobuz_creds_cache_path",
    "_load_qobuz_creds",
    "_save_qobuz_creds",
    "_qobuz_creds_fresh",
    "_scrape_qobuz_open_credentials",
    "_qobuz_creds_valid",
    "_get_qobuz_api_credentials",
    "_qobuz_signed_params",
    "_do_qobuz_signed_json_request",
    "_resolve_qobuz_by_isrc",
    "_get_qobuz_track",
    "_parse_qobuz_track",
    "_get_qobuz_stream_url",
    "_QOBUZ_STREAM_PROXIES",
    "_QOBUZ_QUALITY_CHAIN",
    "_get_qobuz_creds",
]

_QOBUZ_API_BASE           = "https://www.qobuz.com/api.json/0.2"
_QOBUZ_DEFAULT_APP_ID     = "712109809"
_QOBUZ_DEFAULT_APP_SECRET = "589be88e4538daea11f509d29e4a23b1"
_QOBUZ_OPEN_PROBE_URL     = "https://open.qobuz.com/track/1"
_QOBUZ_CREDS_CACHE_TTL    = 24 * 3600
_QOBUZ_PROBE_ISRC         = "USUM71703861"
_QOBUZ_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_QOBUZ_STREAM_PROXIES = [
    "https://dab.yeet.su/api/stream?trackId={id}&quality={q}",
    "https://dabmusic.xyz/api/stream?trackId={id}&quality={q}",
    "https://qobuz.spotbye.qzz.io/api/track/{id}?quality={q}",
    "https://qobuz2.spotbye.qzz.io/api/track/{id}?quality={q}",
]

_QOBUZ_QUALITY_CHAIN: dict = {
    "flac_hi": [27, 7, 6],
    "flac_cd": [6, 7],
    "mp3":     [],
}

_qobuz_bundle_re  = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
_qobuz_app_id_re     = re.compile(r'"?app_id"?\s*[:=]\s*"?(\d{7,12})"?')
_qobuz_app_secret_re = re.compile(r'"?app_secret"?\s*[:=]\s*"?([a-f0-9]{32})"?')

_qobuz_creds_lock  = threading.Lock()
_qobuz_creds_cache: dict | None = None


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
    try:
        resp = requests.get(
            _QOBUZ_OPEN_PROBE_URL,
            headers={"User-Agent": _QOBUZ_UA},
            timeout=20,
        )
        if not resp.ok:
            return None
        m = _qobuz_bundle_re.search(resp.text)
        if not m:
            return None
        bundle_url = m.group(1)
        if bundle_url.startswith("/"):
            bundle_url = "https://open.qobuz.com" + bundle_url
        bundle_resp = requests.get(bundle_url, headers={"User-Agent": _QOBUZ_UA}, timeout=30)
        if not bundle_resp.ok:
            return None
        bundle_text = bundle_resp.text
        m_id  = _qobuz_app_id_re.search(bundle_text)
        m_sec = _qobuz_app_secret_re.search(bundle_text)
        if not m_id or not m_sec:
            return None
        creds = {
            "app_id":     m_id.group(1),
            "app_secret": m_sec.group(1),
            "source":     bundle_url,
            "fetched_at": time.time(),
        }
        return creds
    except Exception as exc:
        log.warning("qobuz credential scraping failed: %s", exc)
        return None


def _qobuz_creds_valid(creds: dict | None) -> bool:
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
            return scraped
        if disk:
            _qobuz_creds_cache = disk
            return disk
        if _qobuz_creds_cache:
            return _qobuz_creds_cache
        fallback = {
            "app_id":     _QOBUZ_DEFAULT_APP_ID,
            "app_secret": _QOBUZ_DEFAULT_APP_SECRET,
            "source":     "embedded-default",
            "fetched_at": time.time(),
        }
        _qobuz_creds_cache = fallback
        return fallback


def _qobuz_signed_params(path: str, params: dict, creds: dict) -> dict:
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

    # MD5 is mandated by the Qobuz API wire protocol for request signing — not our choice.
    sig = hashlib.md5(payload.encode()).hexdigest()  # noqa: S324
    out = dict(params)
    out["app_id"]      = creds["app_id"]
    out["request_ts"]  = timestamp
    out["request_sig"] = sig
    return out


def _do_qobuz_signed_json_request(path: str, params: dict) -> dict:
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


def _resolve_qobuz_by_isrc(isrc: str) -> dict | None:
    if isrc.startswith("qobuz_"):
        return _get_qobuz_track(isrc[len("qobuz_"):])
    try:
        data = _do_qobuz_signed_json_request("track/search", {"query": isrc, "limit": "5"})
        tracks = (data.get("tracks") or {}).get("items") or []
        for t in tracks:
            if (t.get("isrc") or "").upper() == isrc.upper():
                return t
    except Exception as exc:
        log.warning("qobuz ISRC lookup: %s", exc)
    return None


def _get_qobuz_track(track_id: str) -> dict | None:
    try:
        data = _do_qobuz_signed_json_request("track/get", {"track_id": str(track_id)})
        if data.get("id") and not data.get("message"):
            return data
    except Exception as exc:
        log.warning("qobuz track get: %s", exc)
    return None


def _parse_qobuz_track(data: dict) -> dict:
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


def _get_qobuz_stream_url(qobuz_track_id: str, quality: int = 6) -> str | None:
    for tmpl in _QOBUZ_STREAM_PROXIES:
        url = tmpl.replace("{id}", str(qobuz_track_id)).replace("{q}", str(quality))
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.ok:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    data = resp.json()
                    stream_url = (
                        data.get("url") or data.get("stream_url") or
                        (data.get("data") or {}).get("url", "")
                    )
                    if stream_url and stream_url.startswith("http"):
                        return stream_url
                elif resp.text.strip().startswith("http"):
                    return resp.text.strip()
        except Exception as exc:
            log.debug("qobuz proxy %s: %s", url, exc)
    return None


# Alias for backward compatibility
_get_qobuz_creds = _get_qobuz_api_credentials
