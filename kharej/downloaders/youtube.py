"""YouTube downloader adapter for the Kharej VPS worker.

Will wrap yt-dlp to download single videos, audio-only tracks, and shorts.
Supports quality selection (best, 1080p, 720p, audio-only) and passes the
result path back to the dispatcher for S2 upload.

# TODO(step-7): implement
"""

from __future__ import annotations
