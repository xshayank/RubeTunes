from __future__ import annotations

"""
Circuit breaker for download providers.

State machine per (service, provider): closed → open → half_open → closed.
In-memory only; resets to closed on restart.

NOTE: ``_is_circuit_open`` and ``_record_provider_outcome`` look up
``sys.modules['spotify_dl'].time`` at call time so that unit tests using
``unittest.mock.patch("spotify_dl.time")`` work correctly.
"""

import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("spotify_dl")

__all__ = [
    "CIRCUIT_FAIL_THRESHOLD",
    "CIRCUIT_FAIL_WINDOW_SEC",
    "CIRCUIT_OPEN_DURATION_SEC",
    "_CB_STATE_CLOSED",
    "_CB_STATE_OPEN",
    "_CB_STATE_HALF_OPEN",
    "_circuit_breakers",
    "_circuit_lock",
    "_cb_key",
    "_is_circuit_open",
    "_record_provider_outcome",
    "_prioritize_providers",
    "get_breaker_states",
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CIRCUIT_FAIL_THRESHOLD   = max(1, int(os.getenv("CIRCUIT_FAIL_THRESHOLD",   "3")))
CIRCUIT_FAIL_WINDOW_SEC  = max(1, int(os.getenv("CIRCUIT_FAIL_WINDOW_SEC",  "300")))  # 5 min
CIRCUIT_OPEN_DURATION_SEC = max(1, int(os.getenv("CIRCUIT_OPEN_DURATION_SEC", "600")))  # 10 min

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
_CB_STATE_CLOSED    = "closed"
_CB_STATE_OPEN      = "open"
_CB_STATE_HALF_OPEN = "half_open"

# ---------------------------------------------------------------------------
# In-memory circuit-breaker registry
# ---------------------------------------------------------------------------
_circuit_breakers: dict = {}
_circuit_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Provider stats (disk)
# ---------------------------------------------------------------------------
_PROVIDER_STATS_FILE = Path(tempfile.gettempdir()) / "tele2rub" / "provider_stats.json"
_provider_stats_lock = threading.Lock()


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


def _cb_key(service: str, provider: str) -> str:
    """Return the composite key used to identify a circuit breaker."""
    return f"{service}|{provider}"


def _is_circuit_open(service: str, provider: str) -> bool:
    """Return True when the circuit is open (provider should be skipped)."""
    now = _get_time()
    with _circuit_lock:
        key = _cb_key(service, provider)
        cb = _circuit_breakers.get(key)
        if cb is None:
            return False
        state = cb["state"]
        if state == _CB_STATE_CLOSED:
            return False
        if state == _CB_STATE_OPEN:
            if now - cb.get("opened_at", 0) >= CIRCUIT_OPEN_DURATION_SEC:
                cb["state"] = _CB_STATE_HALF_OPEN
                log.info(
                    "circuit breaker [%s] → half_open (after %ds open)",
                    key, CIRCUIT_OPEN_DURATION_SEC,
                )
                return False  # allow the next request through
            return True  # still open — skip
        # half_open: let exactly one request through
        return False


def _record_provider_outcome(
    service: str,
    provider: str,
    success: bool,
    reason: str = "",
    *,
    force_open: bool = False,
) -> None:
    """Record a provider outcome and manage circuit-breaker state transitions.

    *force_open* immediately opens the circuit (used for 429 responses).
    """
    now = _get_time()
    key = _cb_key(service, provider)

    # Update disk-based priority stats
    with _provider_stats_lock:
        stats = _load_provider_stats()
        entry = stats.get(key, {"success": 0, "failure": 0, "last_success": 0})
        if success:
            entry["success"] = entry.get("success", 0) + 1
            entry["last_success"] = now
        else:
            entry["failure"] = entry.get("failure", 0) + 1
        stats[key] = entry
        _save_provider_stats(stats)

    # Update in-memory circuit-breaker state
    with _circuit_lock:
        cb = _circuit_breakers.setdefault(key, {
            "state":                _CB_STATE_CLOSED,
            "consecutive_failures": 0,
            "last_failure_ts":      0.0,
            "opened_at":            0.0,
            "last_reason":          "",
        })
        state = cb["state"]

        if success:
            if state in (_CB_STATE_HALF_OPEN, _CB_STATE_OPEN):
                log.info("circuit breaker [%s] → closed (success in %s)", key, state)
            cb["state"] = _CB_STATE_CLOSED
            cb["consecutive_failures"] = 0
        else:
            in_window = (now - cb.get("last_failure_ts", 0)) < CIRCUIT_FAIL_WINDOW_SEC
            if in_window:
                cb["consecutive_failures"] = cb.get("consecutive_failures", 0) + 1
            else:
                cb["consecutive_failures"] = 1
            cb["last_failure_ts"] = now
            cb["last_reason"]     = reason or "unknown"

            if force_open or state == _CB_STATE_HALF_OPEN:
                cb["state"]     = _CB_STATE_OPEN
                cb["opened_at"] = now
                log.warning("circuit breaker [%s] → open (%s)", key, reason or "forced")
            elif (
                state == _CB_STATE_CLOSED
                and cb["consecutive_failures"] >= CIRCUIT_FAIL_THRESHOLD
            ):
                cb["state"]     = _CB_STATE_OPEN
                cb["opened_at"] = now
                log.warning(
                    "circuit breaker [%s] → open (%d consecutive failures within %ds, last: %s)",
                    key, cb["consecutive_failures"], CIRCUIT_FAIL_WINDOW_SEC, cb["last_reason"],
                )


def _prioritize_providers(service: str, providers: list) -> list:
    """Sort providers by most recent success, skipping any with an open circuit."""
    try:
        stats = _load_provider_stats()
        available = [p for p in providers if not _is_circuit_open(service, p)]
        if not available:
            log.debug("all circuits open for service=%s; ignoring breakers", service)
            available = list(providers)

        def score(p: str) -> float:
            entry = stats.get(f"{service}|{p}", {})
            return entry.get("last_success", 0.0)

        return sorted(available, key=score, reverse=True)
    except Exception:
        return providers


def get_breaker_states() -> list:
    """Return the current circuit-breaker state for every tracked provider."""
    now = _get_time()
    with _circuit_lock:
        snap = dict(_circuit_breakers)

    result = []
    for key, cb in snap.items():
        service, _, provider = key.partition("|")
        secs_until_close = 0
        if cb["state"] == _CB_STATE_OPEN:
            elapsed = now - cb.get("opened_at", now)
            secs_until_close = max(0, CIRCUIT_OPEN_DURATION_SEC - elapsed)
        result.append({
            "key":                  key,
            "service":              service,
            "provider":             provider,
            "state":                cb["state"],
            "consecutive_failures": cb.get("consecutive_failures", 0),
            "last_failure_ts":      cb.get("last_failure_ts", 0.0),
            "opened_at":            cb.get("opened_at", 0.0),
            "seconds_until_close":  secs_until_close,
            "last_reason":          cb.get("last_reason", ""),
        })
    result.sort(key=lambda r: r["key"])
    return result
