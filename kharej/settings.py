"""Runtime settings loader for the Kharej VPS worker.

This module will centralise all configuration consumed by the worker process,
using Pydantic ``BaseSettings`` to validate and coerce environment variables:
- Arvan S2 credentials and bucket name.
- Rubika session token / account credentials.
- Concurrency limits (max parallel downloads, queue depth).
- Feature flags (e.g. enable/disable specific downloader platforms).
- Prometheus metrics port.

Settings will be loaded once at startup and injected into dependent modules
to avoid scattered ``os.getenv`` calls throughout the codebase.

# TODO(step-5): implement
"""

from __future__ import annotations
