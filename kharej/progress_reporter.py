"""Progress reporter for the Kharej VPS worker.

This module will coalesce download progress callbacks from the individual
downloader modules and emit structured ``job.progress`` events back to the
Iran VPS via ``rubika_client``.  Key responsibilities:
- Throttle progress updates (e.g. at most one update per second per job) to
  avoid flooding the Rubika channel.
- Include percentage, ETA, and current download speed in each event.
- Emit a final ``job.done`` or ``job.error`` event upon completion.
- Expose a Prometheus gauge for active-download count and a histogram for
  end-to-end job latency.

# TODO(step-5): implement
"""

from __future__ import annotations
