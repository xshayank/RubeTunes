"""Arvan S2 Object Storage client for the Kharej VPS.

This module will provide an async wrapper around boto3 (S3-compatible API) to:
- Upload downloaded media files to a designated Arvan S2 bucket.
- Generate pre-signed GET URLs so the Iran Web UI can stream/download files.
- Manage object lifecycle (TTL, deletion after delivery confirmation).
- Implement exponential-backoff retry via ``tenacity`` and emit Prometheus
  metrics for upload latency and error rates.

# TODO(step-3): implement
"""

from __future__ import annotations
