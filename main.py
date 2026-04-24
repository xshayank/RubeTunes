"""RubeTunes entry point.

Starts rub.py as a subprocess and forwards SIGTERM / SIGINT for graceful
shutdown (A8).  Also initialises logging, Sentry, and the Prometheus
metrics endpoint (A5, A6, A7).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Optional: restore a saved queue snapshot from a previous shutdown
# ---------------------------------------------------------------------------
QUEUE_SNAPSHOT = BASE_DIR / "queue_snapshot.json"
if QUEUE_SNAPSHOT.exists():
    try:
        with QUEUE_SNAPSHOT.open() as _f:
            _snap = json.load(_f)
        print(f"[main] Restoring queue snapshot ({len(_snap)} entries)")
        # The snapshot will be picked up by rub.py on startup via the file.
        # We rename it so rub.py knows to ingest and delete it.
    except Exception as _e:
        print(f"[main] Could not read queue snapshot: {_e}")

# ---------------------------------------------------------------------------
# Setup logging before importing anything else
# ---------------------------------------------------------------------------
try:
    from rubetunes.logging_setup import setup_logging

    setup_logging()
except ImportError:
    import logging

    logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Sentry (A7) — init only if SENTRY_DSN is set
# ---------------------------------------------------------------------------
try:
    from rubetunes.sentry_setup import init_sentry

    init_sentry()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Prometheus metrics (A6)
# ---------------------------------------------------------------------------
try:
    from rubetunes.metrics import start_metrics_server

    start_metrics_server()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Launch rub.py subprocess
# ---------------------------------------------------------------------------
rubika_file = BASE_DIR / "rub.py"
rubika_proc: subprocess.Popen | None = None

_SHUTDOWN_TIMEOUT = int(os.getenv("SHUTDOWN_TIMEOUT_SEC", "30"))


def _graceful_shutdown(signum: int, frame: object) -> None:
    """Handle SIGTERM/SIGINT: allow in-flight downloads to finish, then exit."""
    sig_name = signal.Signals(signum).name
    print(f"[main] Received {sig_name} — initiating graceful shutdown…")

    if rubika_proc and rubika_proc.poll() is None:
        # Forward the signal to the child process
        try:
            rubika_proc.send_signal(signum)
        except ProcessLookupError:
            pass

        # Wait up to _SHUTDOWN_TIMEOUT seconds for the child to finish
        deadline = time.time() + _SHUTDOWN_TIMEOUT
        while time.time() < deadline:
            if rubika_proc.poll() is not None:
                break
            time.sleep(0.5)
        else:
            print(f"[main] Child did not exit within {_SHUTDOWN_TIMEOUT}s — killing")
            rubika_proc.kill()

    print("[main] Exiting cleanly (code 0)")
    sys.exit(0)


signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)

try:
    rubika_proc = subprocess.Popen([sys.executable, str(rubika_file)])
    rubika_proc.wait()
    if rubika_proc.returncode not in (0, -signal.SIGTERM, -signal.SIGINT):
        print(f"[main] rub.py exited with code {rubika_proc.returncode}")
        sys.exit(rubika_proc.returncode)

except KeyboardInterrupt:
    _graceful_shutdown(signal.SIGINT, None)
finally:
    if rubika_proc and rubika_proc.poll() is None:
        rubika_proc.terminate()
