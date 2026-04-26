"""Smoke tests for the kharej package skeleton (Step 1)."""

from __future__ import annotations

import subprocess
import sys


def test_package_imports() -> None:
    """The package must be importable and expose the correct version."""
    import kharej

    assert kharej.__version__ == "0.1.0"


def test_worker_help_runs() -> None:
    """``python -m kharej.worker --help`` must exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0


def test_worker_healthcheck_runs() -> None:
    """``python -m kharej.worker --healthcheck`` must exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--healthcheck"],
        capture_output=True,
    )
    assert result.returncode == 0
