import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

rubika_file = BASE_DIR / "rub.py"

rubika_proc = None

try:
    rubika_proc = subprocess.Popen([sys.executable, str(rubika_file)])
    rubika_proc.wait()

except KeyboardInterrupt:
    pass
finally:
    if rubika_proc and rubika_proc.poll() is None:
        rubika_proc.terminate()
