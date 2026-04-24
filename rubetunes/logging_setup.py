from __future__ import annotations

"""Logging configuration helper."""

import logging
import sys

__all__ = ["setup_logging"]


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("spotify_dl").setLevel(level)
