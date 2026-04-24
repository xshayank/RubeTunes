from __future__ import annotations

"""Logging configuration helper (A5).

Toggle between human-readable text and structured JSON via the LOG_FORMAT
environment variable:
  LOG_FORMAT=text  (default) — standard %(asctime)s … format
  LOG_FORMAT=json            — JSON lines via python-json-logger (if installed)
"""

import logging
import os
import sys

__all__ = ["setup_logging"]

_LOG_FORMAT = os.getenv("LOG_FORMAT", "text").strip().lower()


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger.  Safe to call multiple times (idempotent)."""
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stderr)

    if _LOG_FORMAT == "json":
        try:
            from pythonjsonlogger import jsonlogger  # type: ignore[import]

            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s"
                )
            )
        except ImportError:
            # python-json-logger not installed — fall back to text
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
            )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    root.addHandler(handler)
    root.setLevel(level)

