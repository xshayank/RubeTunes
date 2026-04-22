# -*- coding: utf-8 -*-
import os
import re
import asyncio
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from rubpy import Client as RubikaClient, filters


load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

SESSION = os.getenv("RUBIKA_SESSION", "rubika_session").strip()
PHONE_NUMBER = os.getenv("RUBIKA_PHONE", "").strip() or None

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


app = RubikaClient(name=SESSION, display_welcome=True)


@app.on_message_updates(filters.commands("start", prefixes="!"))
async def start_handler(update):
    print("[rub] !start received")
    await app.send_message(
        update.object_guid,
        "🎬 Welcome!\n\nAvailable commands (send these to Saved Messages):\n"
        "!download <url> — Download a YouTube video\n"
        "!start — Show this message"
    )


@app.on_message_updates(filters.commands("download", prefixes="!"))
async def download_handler(update):
    log = logging.getLogger("download")
    log.info("!download received | text=%r | object_guid=%s", update.text, update.object_guid)

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    log.info("parsed args: %r", args)
    match = YOUTUBE_RE.search(args)
    object_guid = update.object_guid

    if not match:
        log.warning("no valid YouTube URL found in args")
        await app.send_message(
            object_guid,
            "❌ Please provide a valid YouTube link.\n"
            "Example: !download https://youtu.be/abc123"
        )
        return

    url = match.group(0)
    log.info("matched URL: %s", url)
    status = await app.send_message(object_guid, "\u23f3 Starting download...")
    status_id = status.message_id
    log.info("status message sent | message_id=%s", status_id)

    cmd = build_ytdlp_cmd(url)
    log.info("yt-dlp command: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log.info("yt-dlp process started | pid=%s", proc.pid)

        downloaded_file = None
        last_update = 0.0

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log.debug("yt-dlp | %s", line)

            if line and not line.startswith("[") and line.endswith(".mp4"):
                downloaded_file = line
                log.info("filepath detected from yt-dlp output: %s", downloaded_file)
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
                log.info("progress: %.1f%% @ %s", percent, speed)
                try:
                    await app.edit_message(
                        object_guid, status_id,
                        f"📥 Downloading...\n[{bar}] {percent:.1f}%\n\u26a1 Speed: {speed}"
                    )
                except Exception as edit_err:
                    log.warning("edit_message (progress) failed: %s", edit_err)

        await proc.wait()
        log.info("yt-dlp exited | returncode=%s", proc.returncode)

        if proc.returncode != 0:
            log.error("yt-dlp failed with returncode %s", proc.returncode)
            await app.edit_message(object_guid, status_id, "❌ Download failed. Please try again.")
            return

        if not downloaded_file:
            log.warning("filepath not captured from output — scanning %s for newest .mp4", DOWNLOAD_DIR)
            mp4s = list(DOWNLOAD_DIR.glob("*.mp4"))
            log.info("found %d mp4 file(s): %s", len(mp4s), [str(p) for p in mp4s])
            if mp4s:
                downloaded_file = str(max(mp4s, key=lambda p: p.stat().st_mtime))
                log.info("selected file by mtime: %s", downloaded_file)

        if not downloaded_file or not Path(downloaded_file).exists():
            log.error("downloaded file not found: %r", downloaded_file)
            await app.edit_message(object_guid, status_id, "❌ Downloaded file not found.")
            return

        file_size = Path(downloaded_file).stat().st_size
        log.info("sending file: %s (%.2f MB)", downloaded_file, file_size / 1024 / 1024)
        await app.edit_message(object_guid, status_id, "\u2705 Download complete. Sending...")
        await app.send_document(object_guid, downloaded_file, caption=url)
        log.info("file sent successfully")

        try:
            Path(downloaded_file).unlink()
            log.info("temp file removed: %s", downloaded_file)
        except Exception as unlink_err:
            log.warning("could not remove temp file: %s", unlink_err)

        await app.edit_message(object_guid, status_id, "\u2705 Done! File sent.")

    except Exception as e:
        log.exception("unhandled exception in download_handler: %s", e)
        try:
            await app.edit_message(object_guid, status_id, f"❌ Error: {str(e)}")
        except Exception:
            pass


if __name__ == "__main__":
    print("[rub] Connecting to Rubika...")
    try:
        app.run(phone_number=PHONE_NUMBER)
    except Exception as exc:
        print(f"[rub] Connection failed: {exc}")
        raise
    print("[rub] Disconnected.")
