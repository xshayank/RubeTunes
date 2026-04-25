"""Upload-retry queue for RubeTunes.

When ``app.send_document(...)`` raises during the upload phase, callers
should use :func:`enqueue_failed_upload` instead of immediately giving up.
The file is moved to a persistent retry directory and a JSON record is
appended to the queue file.  A background asyncio task (started via
:func:`start_retry_loop`) retries each entry every
``UPLOAD_RETRY_INTERVAL_SECONDS`` until it succeeds or until
``UPLOAD_RETRY_MAX_ATTEMPTS`` is exhausted.

Environment variables
---------------------
UPLOAD_RETRY_INTERVAL_SECONDS
    How often the retry loop runs (default 3600 = 1 h).
UPLOAD_RETRY_MAX_ATTEMPTS
    Give up after this many failed attempts (default 168 ≈ 1 week).
UPLOAD_RETRY_DIR
    Root directory for files waiting to be retried.
    Defaults to ``<repo>/downloads/.upload_retry``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("upload_retry")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent.parent  # repo root

UPLOAD_RETRY_INTERVAL_SECONDS: int = int(
    os.getenv("UPLOAD_RETRY_INTERVAL_SECONDS", "3600")
)
UPLOAD_RETRY_MAX_ATTEMPTS: int = int(
    os.getenv("UPLOAD_RETRY_MAX_ATTEMPTS", "168")
)
UPLOAD_RETRY_DIR: Path = Path(
    os.getenv("UPLOAD_RETRY_DIR", str(_BASE_DIR / "downloads" / ".upload_retry"))
).resolve()

_QUEUE_FILE: Path = _BASE_DIR / "upload_retry_queue.json"

# Attempt numbers on which to notify the user after the initial failure.
# Roughly: 1st retry (2), 5th retry (6), 24th retry, 168th retry.
_NOTIFY_ATTEMPTS: frozenset[int] = frozenset({1, 2, 6, 24, 168})

# ---------------------------------------------------------------------------
# In-memory queue — protected by a simple asyncio.Lock
# ---------------------------------------------------------------------------

_queue: list[dict[str, Any]] = []
_queue_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_queue() -> list[dict[str, Any]]:
    """Load queue from disk; returns empty list on any error."""
    if not _QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as exc:
        log.warning("upload_retry: failed to load queue: %s", exc)
    return []


def _save_queue(entries: list[dict[str, Any]]) -> None:
    """Atomically write the queue to disk (`.tmp` + rename)."""
    tmp = _QUEUE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_QUEUE_FILE)
    except Exception as exc:
        log.error("upload_retry: failed to save queue: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_on_startup() -> None:
    """Restore the queue from disk.  Call once at bot startup (sync context)."""
    loaded = _load_queue()
    _queue.clear()
    _queue.extend(loaded)
    # Prune entries whose file no longer exists (e.g. manual cleanup)
    _queue[:] = [e for e in _queue if Path(e["file_path"]).exists()]
    if _queue:
        log.info("upload_retry: restored %d pending entries", len(_queue))


def enqueue_failed_upload(
    *,
    app_send_document,  # the callable (stored for test injection)
    object_guid: str,
    file_path: Path,
    file_name: str,
    caption: str,
    provider: str,
    exc: Exception,
) -> str:
    """Move *file_path* to the retry dir and append a queue entry.

    Returns the new entry's UUID string so callers can surface it to users.
    This is a **synchronous** function (safe to call from any context).
    """
    UPLOAD_RETRY_DIR.mkdir(parents=True, exist_ok=True)

    entry_id = str(uuid.uuid4())
    dest_dir = UPLOAD_RETRY_DIR / entry_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / file_name
    try:
        shutil.move(str(file_path), str(dest_file))
    except Exception as move_exc:
        log.error(
            "upload_retry: could not move %s to retry dir: %s", file_path, move_exc
        )
        # If move failed, copy and hope for the best
        try:
            shutil.copy2(str(file_path), str(dest_file))
        except Exception:
            pass

    now = _now_iso()
    entry: dict[str, Any] = {
        "id": entry_id,
        "object_guid": object_guid,
        "file_path": str(dest_file),
        "file_name": file_name,
        "caption": caption,
        "provider": provider,
        "created_at": now,
        "attempts": 1,
        "last_attempt_at": now,
        "last_error": str(exc),
    }
    _queue.append(entry)
    _save_queue(list(_queue))
    log.info(
        "upload_retry: queued %s | provider=%s | guid=%s | id=%s",
        file_name,
        provider,
        object_guid,
        entry_id,
    )
    return entry_id


async def _attempt_entry(entry: dict[str, Any], app) -> bool:
    """Try to upload one queue entry.  Returns True on success."""
    fp = Path(entry["file_path"])
    if not fp.exists():
        log.warning("upload_retry: file gone for entry %s, dropping", entry["id"])
        return True  # treat as "done" — drop from queue

    try:
        await app.send_document(
            entry["object_guid"],
            str(fp),
            caption=entry.get("caption", ""),
            file_name=entry["file_name"],
        )
        return True
    except Exception as exc:
        entry["last_error"] = str(exc)
        return False


async def run_retry_tick(app) -> None:
    """One tick of the retry loop: try every pending entry once."""
    async with _queue_lock:
        if not _queue:
            return
        # Snapshot — new entries added during iteration are picked up next tick
        snapshot = list(_queue)

    to_remove: list[str] = []
    now = _now_iso()

    for entry in snapshot:
        entry_id = entry["id"]
        fp = Path(entry["file_path"])

        try:
            success = await _attempt_entry(entry, app)
        except Exception as exc:
            success = False
            entry["last_error"] = str(exc)

        if success:
            # Notify user of success
            try:
                await app.send_message(
                    entry["object_guid"],
                    f"✅ Delayed upload succeeded: {entry['file_name']}",
                )
            except Exception:
                pass
            # Clean up retry file and parent dir
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                fp.parent.rmdir()
            except Exception:
                pass
            to_remove.append(entry_id)
            log.info(
                "upload_retry: succeeded entry %s (%s) after %d attempts",
                entry_id,
                entry["file_name"],
                entry["attempts"],
            )
        else:
            entry["attempts"] += 1
            entry["last_attempt_at"] = now

            attempts = entry["attempts"]

            if attempts > UPLOAD_RETRY_MAX_ATTEMPTS:
                # Give up permanently
                try:
                    await app.send_message(
                        entry["object_guid"],
                        f"❌ Upload of {entry['file_name']} permanently failed after "
                        f"{attempts - 1} attempts. Last error: {entry['last_error']}",
                    )
                except Exception:
                    pass
                try:
                    fp.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    fp.parent.rmdir()
                except Exception:
                    pass
                to_remove.append(entry_id)
                log.warning(
                    "upload_retry: giving up entry %s (%s) after %d attempts",
                    entry_id,
                    entry["file_name"],
                    attempts - 1,
                )
            else:
                # Notify on select attempt milestones to avoid spam
                if attempts in _NOTIFY_ATTEMPTS:
                    try:
                        await app.send_message(
                            entry["object_guid"],
                            f"⚠️ Still retrying upload of {entry['file_name']} "
                            f"(attempt {attempts}). Last error: {entry['last_error']}",
                        )
                    except Exception:
                        pass

    if to_remove:
        async with _queue_lock:
            _queue[:] = [e for e in _queue if e["id"] not in to_remove]
            _save_queue(list(_queue))


async def _retry_loop(app) -> None:
    """Background asyncio task: run :func:`run_retry_tick` every interval."""
    log.info(
        "upload_retry: loop started (interval=%ds, max_attempts=%d)",
        UPLOAD_RETRY_INTERVAL_SECONDS,
        UPLOAD_RETRY_MAX_ATTEMPTS,
    )
    while True:
        await asyncio.sleep(UPLOAD_RETRY_INTERVAL_SECONDS)
        try:
            await run_retry_tick(app)
        except Exception as exc:
            log.exception("upload_retry: unexpected error in retry tick: %s", exc)


def start_retry_loop(app) -> asyncio.Task:
    """Schedule the background retry loop.  Call after the event loop is running."""
    return asyncio.ensure_future(_retry_loop(app))


# ---------------------------------------------------------------------------
# !uploads command helpers
# ---------------------------------------------------------------------------


def list_entries() -> list[dict[str, Any]]:
    """Return a shallow copy of the current queue."""
    return list(_queue)


def cancel_entry(entry_id: str) -> bool:
    """Remove an entry by ID.  Returns True if it was found and removed."""
    for i, entry in enumerate(_queue):
        if entry["id"] == entry_id:
            fp = Path(entry["file_path"])
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                fp.parent.rmdir()
            except Exception:
                pass
            _queue.pop(i)
            _save_queue(list(_queue))
            return True
    return False
