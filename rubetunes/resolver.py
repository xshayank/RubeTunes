from __future__ import annotations

"""Multi-platform resolver helpers."""

import concurrent.futures
import json
import logging
import re
import threading
import time
from urllib.parse import urlparse as _urlparse

import requests

log = logging.getLogger("spotify_dl")

__all__ = [
    "_resolution_pool",
    "_resolve_via_odesli",
    "_resolve_via_songstats",
    "_musicbrainz_genre",
    "_resolve_all_platforms",
    "get_track_info",
    "get_tidal_track_info",
    "get_qobuz_track_info",
]

_resolution_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="resolve"
)

_mb_lock = threading.Lock()
_mb_last_call = 0.0


def _resolve_via_odesli(track_url: str) -> dict:
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
        qobuz_link = links.get("qobuz") or links.get("qobuzStore")
        if qobuz_link:
            result["qobuz_url"] = qobuz_link.get("url", "")
        amazon_link = links.get("amazonMusic") or links.get("amazon")
        if amazon_link:
            result["amazon_url"] = amazon_link.get("url", "")
        if "spotify" in links:
            result["spotify_url"] = links["spotify"]["url"]
        return result
    except Exception as exc:
        log.warning("odesli resolve: %s", exc)
    return {}


def _resolve_via_songstats(isrc: str) -> dict:
    try:
        url = f"https://songstats.com/track/{isrc.upper()}"
        resp = requests.get(
            url,
            params={"ref": "ISRCFinder"},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "Accept": "text/html"},
            timeout=15,
        )
        if not resp.ok:
            return {}
        html_text = resp.text
        result: dict = {}
        for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.S):
            try:
                obj = json.loads(block)
                same_as = obj.get("sameAs") or []
                if isinstance(same_as, str):
                    same_as = [same_as]
                for u in same_as:
                    try:
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
        return result
    except Exception as exc:
        log.warning("songstats resolve: %s", exc)
    return {}


def _musicbrainz_genre(isrc: str, max_genres: int = 3) -> str:
    global _mb_last_call
    if not isrc:
        return ""
    try:
        with _mb_lock:
            wait = 1.1 - (time.time() - _mb_last_call)
            if wait > 0:
                time.sleep(wait)
            _mb_last_call = time.time()
        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": f"isrc:{isrc}", "fmt": "json", "inc": "tags", "limit": "1"},
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
        return ", ".join(t["name"].title() for t in tags[:max_genres] if t.get("name"))
    except Exception as exc:
        log.debug("musicbrainz genre lookup: %s", exc)
        return ""


def _resolve_all_platforms(info: dict) -> dict:
    # Import providers lazily to avoid circular-import issues
    from rubetunes.providers.deezer import _resolve_deezer, _deezer_url_from_isrc, _deezer_isrc_from_url
    from rubetunes.providers.qobuz import _resolve_qobuz_by_isrc
    from rubetunes.providers.tidal import _resolve_tidal_by_isrc
    from rubetunes.providers.tidal_alt import _get_tidal_alt_url
    from rubetunes.spotify_meta import parse_spotify_track_id

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

    def _fetch_deezer():
        try: return _resolve_deezer(isrc)
        except Exception: return None

    def _fetch_qobuz():
        try: return _resolve_qobuz_by_isrc(isrc)
        except Exception: return None

    def _fetch_tidal():
        try: return _resolve_tidal_by_isrc(isrc)
        except Exception: return None

    def _fetch_tidal_alt():
        if not spotify_id:
            return None
        try: return _get_tidal_alt_url(spotify_id)
        except Exception: return None

    odesli_input = (
        (f"https://open.spotify.com/track/{spotify_id}" if spotify_id else None)
        or info.get("deezer_url")
        or info.get("tidal_url")
        or info.get("qobuz_url")
    )

    def _fetch_odesli():
        try:
            if odesli_input:
                return _resolve_via_odesli(odesli_input)
        except Exception:
            pass
        return {}

    f_dz   = _resolution_pool.submit(_fetch_deezer)
    f_qz   = _resolution_pool.submit(_fetch_qobuz)
    f_td   = _resolution_pool.submit(_fetch_tidal)
    f_talt = _resolution_pool.submit(_fetch_tidal_alt)
    f_od   = _resolution_pool.submit(_fetch_odesli)

    dz   = f_dz.result()
    qz   = f_qz.result()
    td   = f_td.result()
    talt = f_talt.result()
    od   = f_od.result()

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
            info["cover_url"] = dz.get("album", {}).get("cover_xl") or dz.get("album", {}).get("cover_big") or ""

    if qz:
        info["qobuz_id"]          = qz["id"]
        info["qobuz_url"]         = f"https://open.qobuz.com/track/{qz['id']}"
        info["qobuz_bit_depth"]   = qz.get("maximum_bit_depth") or qz.get("bit_depth") or 16
        info["qobuz_sample_rate"] = qz.get("maximum_sampling_rate") or qz.get("sampling_rate") or 44100

    if td:
        info["tidal_id"]  = td["id"]
        info["tidal_url"] = f"https://tidal.com/browse/track/{td['id']}"

    if talt:
        info["tidal_alt_url"] = talt
    elif spotify_id:
        info["tidal_alt_available"] = True

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
        if od.get("spotify_url") and not info.get("track_id"):
            sp_id = parse_spotify_track_id(od["spotify_url"])
            if sp_id:
                info["track_id"] = sp_id
                if not info.get("tidal_alt_url"):
                    tidal_alt_url = _get_tidal_alt_url(sp_id)
                    if tidal_alt_url:
                        info["tidal_alt_url"] = tidal_alt_url
                    else:
                        info["tidal_alt_available"] = True

    if isrc and (not info["tidal_url"] or not info["amazon_url"]):
        sg = _resolve_via_songstats(isrc)
        if sg.get("tidal_url") and not info["tidal_url"]:
            info["tidal_url"] = sg["tidal_url"]
        if sg.get("deezer_url") and not info["deezer_url"]:
            info["deezer_url"] = sg["deezer_url"]
        if sg.get("amazon_url") and not info["amazon_url"]:
            info["amazon_url"] = sg["amazon_url"]

    if isrc and not info["deezer_url"]:
        dz_url = _deezer_url_from_isrc(isrc)
        if dz_url:
            info["deezer_url"] = dz_url

    if isrc and not info.get("genre"):
        genre = _musicbrainz_genre(isrc)
        if genre:
            info["genre"] = genre

    return info


def get_track_info(track_id: str) -> dict:
    from rubetunes.cache import _cache_get_track_info, _cache_set_track_info
    from rubetunes.cache import _get_cached_isrc, _put_cached_isrc
    from rubetunes.spotify_meta import (
        _fetch_track_graphql, _parse_graphql_track,
        _fetch_internal_meta, _parse_internal,
        _fetch_public_meta, _parse_public,
        _isrc_soundplate,
        _auth_headers, get_lyrics,
    )
    from rubetunes.providers.deezer import _upgrade_spotify_cover_url
    from rubetunes.providers.deezer import _deezer_isrc_from_url
    import threading as _threading
    import requests as _requests

    cached = _cache_get_track_info(track_id)
    if cached is not None:
        return cached

    info: dict = {}
    cached_isrc = _get_cached_isrc(track_id)
    if cached_isrc:
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

    _internal_raw: dict | None = None
    try:
        raw = _fetch_track_graphql(track_id)
        info = _parse_graphql_track(raw)
    except Exception as exc:
        log.warning("graphql meta failed (%s) — trying spclient", exc)
        try:
            _internal_raw = _fetch_internal_meta(track_id)
            info = _parse_internal(_internal_raw)
        except Exception as exc2:
            log.warning("internal meta failed (%s) — trying public API", exc2)
            try:
                raw = _fetch_public_meta(track_id)
                info = _parse_public(raw)
            except Exception as exc3:
                log.error("public meta also failed: %s", exc3)
                info = {
                    "title": "", "artists": [], "album": "",
                    "release_date": "", "cover_url": "",
                    "track_number": 1, "disc_number": 1, "isrc": None,
                }

    info["track_id"] = track_id
    if info.get("cover_url"):
        info["cover_url"] = _upgrade_spotify_cover_url(info["cover_url"])
    if not info.get("isrc"):
        info["isrc"] = _isrc_soundplate(track_id)
    if info.get("isrc"):
        _put_cached_isrc(track_id, info["isrc"])

    if _internal_raw and not info.get("upc"):
        try:
            album_gid_bytes = (_internal_raw.get("album") or {}).get("gid")
            if album_gid_bytes:
                album_gid_hex = album_gid_bytes.hex() if isinstance(album_gid_bytes, (bytes, bytearray)) else str(album_gid_bytes).lower()
                album_meta_resp = _requests.get(
                    f"https://spclient.wg.spotify.com/metadata/4/album/{album_gid_hex}?market=from_token",
                    headers=_auth_headers(), timeout=10,
                )
                if album_meta_resp.ok:
                    for eid in (album_meta_resp.json().get("external_id") or []):
                        if eid.get("type") == "upc":
                            info["upc"] = eid.get("id", "")
                            break
        except Exception:
            pass

    info = _resolve_all_platforms(info)

    if not info.get("isrc") and info.get("deezer_url"):
        dz_isrc = _deezer_isrc_from_url(info["deezer_url"])
        if dz_isrc:
            info["isrc"] = dz_isrc
            _put_cached_isrc(track_id, dz_isrc)

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
            except Exception:
                pass
        t = _threading.Thread(target=_bg_lyrics, daemon=True)
        t.start()
        t.join(timeout=15)

    _cache_set_track_info(track_id, info)
    return info


def get_tidal_track_info(track_id: str) -> dict:
    from rubetunes.providers.tidal import TIDAL_TOKEN, _get_tidal_track, _parse_tidal_track
    from rubetunes.providers.tidal_alt import _get_tidal_alt_url
    from rubetunes.providers.deezer import _deezer_isrc_from_url
    from rubetunes.providers.qobuz import _get_qobuz_track, _parse_qobuz_track
    from rubetunes.spotify_meta import parse_spotify_track_id, parse_qobuz_track_id
    import requests as _requests
    import re as _re

    tidal_url = f"https://tidal.com/browse/track/{track_id}"
    if TIDAL_TOKEN:
        data = _get_tidal_track(track_id)
        if data:
            info = _parse_tidal_track(data)
            info["track_id"] = None
            info["tidal_id"] = track_id
            info["tidal_url"] = tidal_url
            return _resolve_all_platforms(info)

    log.info("No TIDAL_TOKEN; resolving Tidal track %s via Odesli", track_id)
    od = _resolve_via_odesli(tidal_url)
    isrc = ""
    if od.get("deezer_url"):
        isrc = _deezer_isrc_from_url(od["deezer_url"]) or ""

    info: dict = {
        "title": "", "artists": [], "album": "",
        "release_date": "", "cover_url": "",
        "track_number": 1, "disc_number": 1,
        "isrc": isrc,
        "track_id": None,
        "tidal_id": track_id,
        "tidal_url": tidal_url,
        "tidal_alt_url": None,
        "tidal_alt_available": True,
        "deezer_id": None, "deezer_url": od.get("deezer_url"),
        "deezer_preview_url": None,
        "qobuz_id": None, "qobuz_url": od.get("qobuz_url"),
        "qobuz_bit_depth": None, "qobuz_sample_rate": None,
        "amazon_url": od.get("amazon_url"),
    }

    if od.get("spotify_url"):
        sp_id = parse_spotify_track_id(od["spotify_url"])
        if sp_id:
            info["track_id"] = sp_id
            tidal_alt = _get_tidal_alt_url(sp_id)
            if tidal_alt:
                info["tidal_alt_url"] = tidal_alt
                info["tidal_alt_available"] = False

    if info.get("qobuz_url"):
        qobuz_id = parse_qobuz_track_id(info["qobuz_url"])
        if qobuz_id:
            info["qobuz_id"] = qobuz_id
            try:
                qz_data = _get_qobuz_track(qobuz_id)
                if qz_data:
                    parsed = _parse_qobuz_track(qz_data)
                    for k in ("title", "artists", "album", "release_date", "cover_url", "track_number", "disc_number", "isrc"):
                        if parsed.get(k) and not info.get(k):
                            info[k] = parsed[k]
                    info["qobuz_bit_depth"]   = qz_data.get("maximum_bit_depth", 16)
                    info["qobuz_sample_rate"] = qz_data.get("maximum_sampling_rate", 44100)
            except Exception as exc:
                log.debug("qobuz metadata for tidal track: %s", exc)

    if info.get("deezer_url") and not info.get("title"):
        try:
            dz_track_id = _re.search(r'/track/(\d+)', info["deezer_url"])
            if dz_track_id:
                resp = _requests.get(f"https://api.deezer.com/track/{dz_track_id.group(1)}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if resp.ok:
                    dz = resp.json()
                    if not info.get("title"):       info["title"] = dz.get("title", "")
                    if not info.get("artists"):     info["artists"] = [dz.get("artist", {}).get("name", "")]
                    if not info.get("album"):       info["album"] = dz.get("album", {}).get("title", "")
                    if not info.get("cover_url"):   info["cover_url"] = dz.get("album", {}).get("cover_xl") or dz.get("album", {}).get("cover_big") or ""
                    if not info.get("isrc"):        info["isrc"] = dz.get("isrc", "")
                    if not info.get("deezer_preview_url"): info["deezer_preview_url"] = dz.get("preview", "")
        except Exception as exc:
            log.debug("deezer metadata for tidal track: %s", exc)

    if not info.get("title") and not info.get("isrc"):
        raise RuntimeError(f"Could not resolve Tidal track {track_id!r}.")

    return info


def get_qobuz_track_info(track_id: str) -> dict:
    from rubetunes.providers.qobuz import _get_qobuz_track, _parse_qobuz_track
    data = _get_qobuz_track(track_id)
    if not data:
        raise RuntimeError(f"Qobuz API returned no data for track {track_id!r}")
    info = _parse_qobuz_track(data)
    info["track_id"]  = None
    info["qobuz_id"]  = track_id
    info["qobuz_url"] = f"https://open.qobuz.com/track/{track_id}"
    return _resolve_all_platforms(info)
