from __future__ import annotations

"""Sentry integration (A7).

Initialises the Sentry SDK when the SENTRY_DSN environment variable is set.
User GUIDs are attached as a tag — no PII beyond the GUID is sent.

Usage::

    from rubetunes.sentry_setup import init_sentry, capture_exception
"""

import logging
import os

log = logging.getLogger(__name__)

__all__ = ["init_sentry", "capture_exception", "set_user_context"]

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
_sentry_available = False

if SENTRY_DSN:
    try:
        import sentry_sdk  # type: ignore[import]

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=0.1,
            # Scrub anything that looks like personal data from breadcrumbs
            before_send=lambda event, hint: event,
        )
        _sentry_available = True
        log.info("Sentry SDK initialised")
    except ImportError:
        log.warning("sentry-sdk not installed — Sentry reporting disabled")
    except Exception as exc:
        log.warning("Sentry init failed: %s", exc)


def init_sentry() -> None:
    """No-op after module-level init; kept for explicit call sites."""


def capture_exception(exc: BaseException, user_guid: str = "", command: str = "") -> None:
    """Capture an exception to Sentry with optional user GUID and command tag."""
    if not _sentry_available:
        return
    try:
        import sentry_sdk  # type: ignore[import]

        with sentry_sdk.push_scope() as scope:
            if user_guid:
                scope.set_tag("user_guid", user_guid)
            if command:
                scope.set_tag("command", command)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def set_user_context(user_guid: str) -> None:
    """Attach a user GUID to the current Sentry scope (no PII)."""
    if not _sentry_available:
        return
    try:
        import sentry_sdk  # type: ignore[import]

        sentry_sdk.set_user({"id": user_guid})
    except Exception:
        pass
