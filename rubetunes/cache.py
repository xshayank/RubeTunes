from __future__ import annotations

"""
LRU track-info cache and ISRC disk cache.

Port of SpotiFLAC backend/recent_fetches.go and isrc_cache utilities.

NOTE: `_cache_get_track_info` and `_cache_set_track_info` look up
``sys.modules['spotify_dl'].time`` at call time so that unit tests using
``unittest.mock.patch("spotify_dl.time")`` work correctly.
"""

import collections
import json
import sys
import tempfile
import threading
from pathlib import Path

import logging

log = logging.getLogger("spotify_dl")

__all__ = [
    "_TRACK_INFO_CACHE_MAX",
    "_TRACK_INFO_CACHE_TTL",
    "_track_info_cache",
    "_track_info_cache_lock",
    "_cache_get_track_info",
    "_cache_set_track_info",
    "clear_track_info_cache",
    "_ISRC_CACHE_FILE",
    "_isrc_cache_lock",
    "_isrc_cache_path",
    "_get_cached_isrc",
    "_put_cached_isrc",
]

# ---------------------------------------------------------------------------
# LRU track-info cache (Gap 6 — SpotiFLAC backend/recent_fetches.go)
# ---------------------------------------------------------------------------
_TRACK_INFO_CACHE_MAX = 256  # max entries
_TRACK_INFO_CACHE_TTL = 600  # seconds (10 min)

_track_info_cache: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
_track_info_cache_lock = threading.Lock()


def _get_time() -> float:
    """Return current time, honouring any ``patch("spotify_dl.time")`` in tests."""
    sdl = sys.modules.get("spotify_dl")
    if sdl is not None:
        t_mod = getattr(sdl, "time", None)
        if t_mod is not None:
            try:
                return t_mod.time()
            except Exception:
                pass
    import time as _time
    return _time.time()


def _cache_get_track_info(track_id: str) -> dict | None:
    """Return cached track info for *track_id*, or None if missing / expired."""
    with _track_info_cache_lock:
        entry = _track_info_cache.get(track_id)
        if entry is None:
            return None
        ts, data = entry
        if _get_time() - ts > _TRACK_INFO_CACHE_TTL:
            _track_info_cache.pop(track_id, None)
            return None
        # LRU: move to end
        _track_info_cache.move_to_end(track_id)
        return data


def _cache_set_track_info(track_id: str, info: dict) -> None:
    """Store *info* in the LRU cache under *track_id*."""
    with _track_info_cache_lock:
        _track_info_cache[track_id] = (_get_time(), info)
        _track_info_cache.move_to_end(track_id)
        while len(_track_info_cache) > _TRACK_INFO_CACHE_MAX:
            _track_info_cache.popitem(last=False)


def clear_track_info_cache() -> int:
    """Clear the in-process track-info LRU cache.  Returns the number of entries cleared."""
    with _track_info_cache_lock:
        n = len(_track_info_cache)
        _track_info_cache.clear()
    log.info("track-info LRU cache cleared (%d entries)", n)
    return n


# ---------------------------------------------------------------------------
# ISRC disk cache (SpotiFLAC GetCachedISRC / PutCachedISRC)
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
