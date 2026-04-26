"""Batch / playlist downloader adapter for the Kharej VPS worker.

Will coordinate multi-track downloads (YouTube playlists, Spotify albums and
playlists) and use ``zip_split`` from the top-level package to split large
archives before S2 upload when a single file exceeds the size limit.

# TODO(step-8): implement
"""

from __future__ import annotations
