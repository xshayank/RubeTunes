from __future__ import annotations

"""Disk space guard (D2).

Before starting a batch download, estimate the required space and reject if
less than MIN_FREE_SPACE_MB (default 2×estimate) is available.

Usage::

    from rubetunes.disk_guard import check_disk_space

    ok, msg = check_disk_space(track_count=50, download_dir=Path("/app/downloads"))
    if not ok:
        await send(user_guid, msg)
        return
"""

import os
import shutil
from pathlib import Path

__all__ = ["check_disk_space", "HEURISTIC_MB_PER_TRACK", "MIN_FREE_SPACE_MB"]

# Heuristic: ~30 MB per FLAC track
HEURISTIC_MB_PER_TRACK: int = int(os.getenv("HEURISTIC_MB_PER_TRACK", "30"))

# Minimum free space multiple: must have at least MIN_FREE_SPACE_MULTIPLIER × estimated
MIN_FREE_SPACE_MULTIPLIER: float = float(os.getenv("MIN_FREE_SPACE_MULTIPLIER", "2.0"))

# Override with an absolute minimum (in MB), regardless of estimate
MIN_FREE_SPACE_MB: int = int(os.getenv("MIN_FREE_SPACE_MB", "500"))


def check_disk_space(
    track_count: int,
    download_dir: Path,
) -> tuple[bool, str]:
    """Check whether there is sufficient disk space for *track_count* tracks.

    Returns (True, "") if OK, or (False, reason_message) if not.
    """
    try:
        usage = shutil.disk_usage(str(download_dir))
    except Exception:
        # If we can't determine disk usage, let it through
        return True, ""

    free_mb = usage.free / (1024 * 1024)
    estimated_mb = track_count * HEURISTIC_MB_PER_TRACK
    required_mb = max(estimated_mb * MIN_FREE_SPACE_MULTIPLIER, float(MIN_FREE_SPACE_MB))

    if free_mb < required_mb:
        return (
            False,
            f"💾 Not enough disk space for this batch.\n"
            f"Estimated: ~{estimated_mb:.0f} MB, "
            f"Required free: {required_mb:.0f} MB, "
            f"Available: {free_mb:.0f} MB.\n"
            f"Please try again later or reduce the batch size.",
        )
    return True, ""
