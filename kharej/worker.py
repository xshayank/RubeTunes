"""Kharej VPS worker.

Responsibilities:
- Consume ``job.create`` control messages from the Rubika control channel.
- Route each job to the appropriate downloader (YouTube, Spotify, Tidal, …).
- Push completed media files to Arvan S2 Object Storage via ``s2_client``.
- Publish lifecycle events (``job.progress``, ``job.done``, ``job.error``) back
  to the Rubika channel so the Iran-side Web UI can update its state.

This module is the main runnable entry point for the Kharej VPS process.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kharej.worker",
        description="RubeTunes Kharej VPS worker — downloads media and pushes to Arvan S2.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Run a liveness check and exit 0.",
    )
    return parser


def main() -> int:
    """CLI entry point.  Returns an exit code."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.healthcheck:
        print("healthcheck stub — not implemented")
        return 0

    print("kharej worker stub — not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
