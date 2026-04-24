from __future__ import annotations

"""Download history helpers.

Port of SpotiFLAC backend/history.go.
"""

import json
import logging
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("spotify_dl")

__all__ = [
    "_DOWNLOAD_HISTORY_PATH",
    "_download_history_lock",
    "_load_download_history",
    "_save_download_history",
    "_history_key",
    "_check_download_history",
    "_record_download_history",
    "get_download_history",
]

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
            fp = Path(entry["file"] if isinstance(entry, dict) else entry)
            if fp.exists() and fp.stat().st_size > 0:
                return fp
    except Exception as exc:
        log.debug("download history check failed: %s", exc)
    return None


def _record_download_history(
    track_id: str,
    source: str,
    quality: str,
    file_path: Path,
    *,
    user_guid: str = "",
    title: str = "",
    artists: str = "",
) -> None:
    """Persist a successful download to history (best-effort, never fatal)."""
    try:
        with _download_history_lock:
            history = _load_download_history()
            history[_history_key(track_id, source, quality)] = {
                "file":      str(file_path),
                "user_guid": user_guid,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "title":     title,
                "artists":   artists,
                "source":    source,
                "quality":   quality,
            }
            _save_download_history(history)
    except Exception as exc:
        log.debug("download history record failed: %s", exc)


def get_download_history() -> list:
    """Return all history entries as a list of dicts, newest first."""
    try:
        with _download_history_lock:
            history = _load_download_history()
        entries = []
        for key, val in history.items():
            if isinstance(val, dict):
                entry = dict(val)
            else:
                parts = key.split("|", 2)
                entry = {
                    "file":      str(val),
                    "user_guid": "",
                    "timestamp": "",
                    "title":     "",
                    "artists":   "",
                    "source":    parts[1] if len(parts) > 1 else "",
                    "quality":   parts[2] if len(parts) > 2 else "",
                }
            entry["_key"] = key
            entries.append(entry)
        entries.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
        return entries
    except Exception as exc:
        log.debug("get_download_history failed: %s", exc)
        return []
