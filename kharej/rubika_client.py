"""Rubika control-channel client for the Kharej VPS.

This module will wrap the Rubika SDK (rubpy or equivalent) and expose a
high-level async interface for:
- Subscribing to ``job.create`` messages sent by the Iran VPS.
- Publishing ``job.progress``, ``job.done``, and ``job.error`` lifecycle
  events back to the shared Rubika group/channel.
- Handling session persistence and automatic reconnection.

Small payloads (search results, video metadata, status updates) travel over
this channel; binary file data is routed exclusively through Arvan S2.

# TODO(step-4): implement
"""

from __future__ import annotations
