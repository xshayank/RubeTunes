"""Spotify downloader adapter for the Kharej VPS worker.

Will orchestrate the multi-provider waterfall (Spotify GraphQL → Tidal alt →
Qobuz → YouTube fallback) already implemented in the top-level
``spotify_dl.py`` shim, and expose a clean async interface for the dispatcher.

# TODO(step-7): implement
"""

from __future__ import annotations
