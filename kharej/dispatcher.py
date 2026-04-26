"""Job dispatcher for the Kharej VPS worker.

This module will inspect each incoming ``job.create`` message and route it to
the correct downloader module based on the ``platform`` field (youtube,
spotify, tidal, qobuz, amazon, soundcloud, bandcamp, musicdl).  It will also:
- Apply the access-control gate before dispatching.
- Track in-flight job state so progress events can reference the originating
  request.
- Enforce concurrency limits and queue depth to avoid overloading the VPS.

# TODO(step-6): implement
"""

from __future__ import annotations
