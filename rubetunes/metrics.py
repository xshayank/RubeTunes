from __future__ import annotations

"""Prometheus metrics endpoint (A6).

Exposes counters, gauges, and histograms at http://:<METRICS_PORT>/metrics.
Set METRICS_PORT=0 to disable.

Usage::

    from rubetunes.metrics import (
        inc_downloads, inc_provider_failures, observe_download_duration,
        set_queue_depth, set_circuit_open,
    )
"""

import logging
import os
import threading

log = logging.getLogger(__name__)

METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

_started = False
_start_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Try to import prometheus_client; fall back to no-ops so the bot works
# without it installed.
# ---------------------------------------------------------------------------
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server  # type: ignore[import]

    _HAVE_PROM = True
except ImportError:
    _HAVE_PROM = False
    log.debug("prometheus_client not installed — metrics endpoint disabled")

__all__ = [
    "start_metrics_server",
    "inc_downloads",
    "inc_provider_failures",
    "inc_resolutions",
    "observe_download_duration",
    "observe_resolution_duration",
    "set_queue_depth",
    "set_circuit_open",
]

if _HAVE_PROM:
    _downloads_total = Counter(
        "rubetunes_downloads_total",
        "Total number of track downloads",
        ["source", "status"],
    )
    _provider_failures_total = Counter(
        "rubetunes_provider_failures_total",
        "Total provider failures",
        ["provider", "reason"],
    )
    _resolutions_total = Counter(
        "rubetunes_resolutions_total",
        "Total resolution attempts",
        ["provider", "outcome"],
    )
    _queue_depth = Gauge(
        "rubetunes_queue_depth",
        "Number of items currently waiting in the download queue",
    )
    _circuit_open = Gauge(
        "rubetunes_circuit_open",
        "1 if the circuit breaker for this provider is open, 0 otherwise",
        ["provider"],
    )
    _download_duration = Histogram(
        "rubetunes_download_duration_seconds",
        "Download duration in seconds",
        ["source"],
        buckets=[1, 5, 10, 30, 60, 120, 300],
    )
    _resolution_duration = Histogram(
        "rubetunes_resolution_duration_seconds",
        "Cross-platform resolution duration in seconds",
        buckets=[0.5, 1, 2, 5, 10, 30],
    )


def start_metrics_server() -> None:
    """Start the Prometheus HTTP server on METRICS_PORT (no-op if port is 0)."""
    global _started
    if not _HAVE_PROM or METRICS_PORT == 0:
        return
    with _start_lock:
        if _started:
            return
        try:
            start_http_server(METRICS_PORT)
            _started = True
            log.info("Prometheus metrics available at http://localhost:%d/metrics", METRICS_PORT)
        except Exception as exc:
            log.warning("Could not start metrics server: %s", exc)


def inc_downloads(source: str, status: str) -> None:
    if _HAVE_PROM:
        _downloads_total.labels(source=source, status=status).inc()


def inc_provider_failures(provider: str, reason: str) -> None:
    if _HAVE_PROM:
        _provider_failures_total.labels(provider=provider, reason=reason).inc()


def inc_resolutions(provider: str, outcome: str) -> None:
    if _HAVE_PROM:
        _resolutions_total.labels(provider=provider, outcome=outcome).inc()


def observe_download_duration(source: str, seconds: float) -> None:
    if _HAVE_PROM:
        _download_duration.labels(source=source).observe(seconds)


def observe_resolution_duration(seconds: float) -> None:
    if _HAVE_PROM:
        _resolution_duration.observe(seconds)


def set_queue_depth(depth: int) -> None:
    if _HAVE_PROM:
        _queue_depth.set(depth)


def set_circuit_open(provider: str, is_open: bool) -> None:
    if _HAVE_PROM:
        _circuit_open.labels(provider=provider).set(1 if is_open else 0)
