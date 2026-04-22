# -*- coding: utf-8 -*-
import os
import re
import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv
from rubpy import Client as RubikaClient, filters


load_dotenv()

SESSION = os.getenv("RUBIKA_SESSION", "rubika_session").strip()

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

YTDLP_BIN = BASE_DIR / "yt-dlp"
COOKIES_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

YOUTUBE_RE = re.compile(
    r'https?://(?:(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/|v/)|youtu\.be/)[\w\-]+'
)

PROGRESS_RE = re.compile(
    r'\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+/s)'
)

UPDATE_INTERVAL = 3.0


def make_bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return '\u2588' * filled + '\u2591' * (width - filled)


def build_ytdlp_cmd(url: str) -> list[str]:
    ytdlp = str(YTDLP_BIN) if YTDLP_BIN.exists() else "yt-dlp"
    cmd = [
        ytdlp,
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "--referer", "https://www.youtube.com/",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format", "mp4",
        "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--newline",
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)
    return cmd


app = RubikaClient(name=SESSION)


@app.on_message_updates(filters.is_me, filters.commands("start", prefixes="!"))
async def start_handler(update):
    await app.send_message(
        update.object_guid,
        "🎬 Welcome!\n\nAvailable commands (send these to Saved Messages):\n"
        "!download <url> — Download a YouTube video\n"
        "!start — Show this message"
    )


@app.on_message_updates(filters.is_me, filters.commands("download", prefixes="!"))
async def download_handler(update):
    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    match = YOUTUBE_RE.search(args)
    object_guid = update.object_guid

    if not match:
        await app.send_message(
            object_guid,
            "❌ Please provide a valid YouTube link.\n"
            "Example: !download https://youtu.be/abc123"
        )
        return

    url = match.group(0)
    status = await app.send_message(object_guid, "\u23f3 Starting download...")
    status_id = status.message_id

    try:
        proc = await asyncio.create_subprocess_exec(
            *build_ytdlp_cmd(url),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        downloaded_file = None
        last_update = 0.0

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()

            if line and not line.startswith("[") and line.endswith(".mp4"):
                downloaded_file = line
                continue

            m = PROGRESS_RE.search(line)
            if not m:
                continue

            percent = float(m.group(1))
            speed = m.group(2)
            now = time.monotonic()

            if now - last_update >= UPDATE_INTERVAL:
                last_update = now
                bar = make_bar(percent)
                try:
                    await app.edit_message(
                        object_guid, status_id,
                        f"📥 Downloading...\n[{bar}] {percent:.1f}%\n\u26a1 Speed: {speed}"
                    )
                except Exception:
                    pass

        await proc.wait()

        if proc.returncode != 0:
            await app.edit_message(object_guid, status_id, "❌ Download failed. Please try again.")
            return

        if not downloaded_file:
            mp4s = list(DOWNLOAD_DIR.glob("*.mp4"))
            if mp4s:
                downloaded_file = str(max(mp4s, key=lambda p: p.stat().st_mtime))

        if not downloaded_file or not Path(downloaded_file).exists():
            await app.edit_message(object_guid, status_id, "❌ Downloaded file not found.")
            return

        await app.edit_message(object_guid, status_id, "\u2705 Download complete. Sending...")
        await app.send_document(object_guid, downloaded_file, caption=url)

        try:
            Path(downloaded_file).unlink()
        except Exception:
            pass

        await app.edit_message(object_guid, status_id, "\u2705 Done! File sent.")

    except Exception as e:
        try:
            await app.edit_message(object_guid, status_id, f"❌ Error: {str(e)}")
        except Exception:
            pass


if __name__ == "__main__":
    app.run()
