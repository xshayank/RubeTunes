import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

telegram_file = BASE_DIR / "telebot.py"
rubika_file = BASE_DIR / "rub.py"

telegram_proc = None
rubika_proc = None

try:
    rubika_proc = subprocess.Popen([sys.executable, str(rubika_file)])
    telegram_proc = subprocess.Popen([sys.executable, str(telegram_file)])

    rubika_proc.wait()
    telegram_proc.wait()

except KeyboardInterrupt:
    pass
finally:
    for proc in [telegram_proc, rubika_proc]:
        if proc and proc.poll() is None:
            proc.terminate()
