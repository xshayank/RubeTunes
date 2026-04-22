# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message


load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
QUEUE_DIR = BASE_DIR / "queue"
QUEUE_FILE = QUEUE_DIR / "tasks.jsonl"

YTDLP_BIN = BASE_DIR / "yt-dlp"
COOKIES_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Please set API_ID, API_HASH and BOT_TOKEN in .env")

app = Client(
    "tel2rub",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

YOUTUBE_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)[\w\-]+'
)

PROGRESS_RE = re.compile(
    r'\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+/s)'
)

UPDATE_INTERVAL = 3.0


def make_bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return '\u2588' * filled + '\u2591' * (width - filled)


def append_task(task: dict) -> None:
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(task, ensure_ascii=False) + "\n")


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


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "🎬 Send me a YouTube link and I will download it and send it to Rubika."
    )


@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def url_handler(client: Client, message: Message):
    text = message.text.strip()
    match = YOUTUBE_RE.search(text)

    if not match:
        await message.reply_text("❌ Please send a valid YouTube link.")
        return

    url = match.group(0)
    status = await message.reply_text("\u23f3 Starting download...")

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

            # yt-dlp prints the final file path via --print after_move:filepath
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
                    await status.edit_text(
                        f"📥 Downloading...\n"
                        f"[{bar}] {percent:.1f}%\n"
                        f"\u26a1 Speed: {speed}"
                    )
                except Exception:
                    pass

        await proc.wait()

        if proc.returncode != 0:
            await status.edit_text("❌ Download failed. Please try again.")
            return

        if not downloaded_file:
            mp4s = list(DOWNLOAD_DIR.glob("*.mp4"))
            if mp4s:
                downloaded_file = str(max(mp4s, key=lambda p: p.stat().st_mtime))

        if not downloaded_file or not Path(downloaded_file).exists():
            await status.edit_text("❌ Downloaded file not found.")
            return

        await status.edit_text("\u2705 Download complete. Queued for sending to Rubika.")

        task = {
            "type": "local_file",
            "path": downloaded_file,
            "caption": url,
            "chat_id": message.chat.id,
            "status_message_id": status.id,
        }
        append_task(task)

    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)}")


if __name__ == "__main__":
    app.run()
