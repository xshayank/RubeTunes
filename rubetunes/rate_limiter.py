from __future__ import annotations

"""Per-user rate limiting (D3).

Non-admin users are limited to USER_TRACKS_PER_HOUR (default 100) track
downloads per rolling hour.  Tracked via an in-memory deque per user GUID.

Usage::

    from rubetunes.rate_limiter import check_rate_limit, record_usage

    allowed, msg = check_rate_limit(user_guid)
    if not allowed:
        await send(user_guid, msg)
        return
    record_usage(user_guid)
"""

import collections
import os
import threading
import time

__all__ = ["check_rate_limit", "record_usage", "USER_TRACKS_PER_HOUR"]

USER_TRACKS_PER_HOUR: int = max(1, int(os.getenv("USER_TRACKS_PER_HOUR", "100")))
_WINDOW_SEC = 3600  # 1 hour rolling window

_usage: dict[str, collections.deque[float]] = {}
_lock = threading.Lock()


def _prune(timestamps: collections.deque[float], now: float) -> None:
    """Remove timestamps older than the rolling window."""
    cutoff = now - _WINDOW_SEC
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()


def check_rate_limit(user_guid: str) -> tuple[bool, str]:
    """Return (allowed, message).

    If the user is under the limit, returns (True, "").
    If they've hit the limit, returns (False, human-readable message).
    """
    now = time.time()
    with _lock:
        dq = _usage.setdefault(user_guid, collections.deque())
        _prune(dq, now)
        if len(dq) >= USER_TRACKS_PER_HOUR:
            # Oldest request in window — show when the user can request again
            oldest = dq[0]
            reset_in = int(oldest + _WINDOW_SEC - now)
            minutes, seconds = divmod(reset_in, 60)
            eta = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            return (
                False,
                f"⏱ Rate limit reached ({USER_TRACKS_PER_HOUR} tracks/hour). "
                f"Try again in {eta}.",
            )
        return True, ""


def record_usage(user_guid: str) -> None:
    """Record one track request for *user_guid*."""
    now = time.time()
    with _lock:
        dq = _usage.setdefault(user_guid, collections.deque())
        _prune(dq, now)
        dq.append(now)


def get_usage_count(user_guid: str) -> int:
    """Return the number of requests *user_guid* has made in the last hour."""
    now = time.time()
    with _lock:
        dq = _usage.get(user_guid, collections.deque())
        _prune(dq, now)
        return len(dq)
