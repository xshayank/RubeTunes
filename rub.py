# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import collections
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from rubpy import Client as RubikaClient, filters

import spotify_dl as _spodl


load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class _SuppressDataEnc(logging.Filter):
    """Drop the noisy 'Missing data_enc key' debug lines from rubpy internals."""
    def filter(self, record):
        return "data_enc" not in record.getMessage()


logging.getLogger("rubpy.network").addFilter(_SuppressDataEnc())

SESSION = os.getenv("RUBIKA_SESSION", "rubika_session").strip()
PHONE_NUMBER = os.getenv("RUBIKA_PHONE", "").strip() or None

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
STATE_FILE = BASE_DIR / "state.json"

# Admin GUIDs loaded from env (comma-separated Rubika object GUIDs or phone numbers
# that identify the admin accounts in private chats).
_raw_admins = os.getenv("ADMIN_GUIDS", "").strip()
ADMIN_GUIDS: set = {g.strip() for g in _raw_admins.split(",") if g.strip()}

YTDLP_BIN = BASE_DIR / "yt-dlp"
COOKIES_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

YOUTUBE_RE = re.compile(
    r'https?://(?:(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/|v/)|youtu\.be/)[\w\-]+'
)

SPOTIFY_RE = re.compile(
    r'https?://open\.spotify\.com/track/([A-Za-z0-9]{22})'
    r'|spotify:track:([A-Za-z0-9]{22})'
    r'|(?<![A-Za-z0-9])([A-Za-z0-9]{22})(?![A-Za-z0-9])'
)

TIDAL_RE = re.compile(
    r'https?://(?:www\.|listen\.)?tidal\.com/(?:browse/)?(?:track|album/[^/\s]+/track)/(\d+)'
    r'|https?://listen\.tidal\.com/album/[^/\s]+/track/(\d+)'
)

QOBUZ_RE = re.compile(
    r'https?://open\.qobuz\.com/track/(\d+)'
    r'|https?://(?:www\.)?qobuz\.com/[^\s]+/track[s]?/(\d+)'
)

AMAZON_RE = re.compile(
    r'https?://music\.amazon\.[a-z.]+/tracks/([A-Z0-9]{10,})'
    r'|[?&]trackAsin=([A-Z0-9]{10,})'
)

PROGRESS_RE = re.compile(
    r'\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+/s)'
)

UPDATE_INTERVAL = 3.0
SELECTION_TIMEOUT = 300.0          # seconds before a pending quality menu expires
SIZE_LIMIT_BYTES = 2 * 1024 ** 3   # 2 GB hard limit — options above this are hidden
MAX_LOG_ENTRIES = 500               # keep the most recent N log lines

# Per-chat pending quality-selection state.
# Key: object_guid  Value: {url, choices, title, timeout_task}
pending_selections: dict = {}

# Download queue -- one download runs at a time.
# Each entry: {object_guid, url, choice, title, queue_msg_id}
download_queue: collections.deque = collections.deque()
is_downloading: bool = False

# ---------------------------------------------------------------------------
# Persistent state: whitelist / ban / usage logs
# ---------------------------------------------------------------------------
# Structure stored in state.json:
#   whitelist_enabled : bool
#   whitelist         : list[str]   – allowed object_guids (when enabled)
#   banned            : list[str]   – permanently blocked object_guids
#   logs              : list[dict]  – usage log entries

_state_lock = asyncio.Lock()

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # Normalise
                data.setdefault("whitelist_enabled", False)
                data.setdefault("whitelist", [])
                data.setdefault("banned", [])
                data.setdefault("logs", [])
                return data
        except Exception:
            pass
    return {"whitelist_enabled": False, "whitelist": [], "banned": [], "logs": []}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# Load into memory at import time; all mutations go through async helpers.
_state: dict = _load_state()


def _is_admin(object_guid: str) -> bool:
    return object_guid in ADMIN_GUIDS


def _check_access(object_guid: str) -> tuple[bool, str]:
    """Return (allowed, reason).  Admins always pass."""
    if _is_admin(object_guid):
        return True, ""
    if object_guid in _state["banned"]:
        return False, "🚫 You have been banned from using this bot."
    if _state["whitelist_enabled"] and object_guid not in _state["whitelist"]:
        return False, "🔒 This bot is currently restricted. Contact the admin."
    return True, ""


def _append_log(object_guid: str, action: str, detail: str = "") -> None:
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "guid": object_guid,
        "action": action,
        "detail": detail,
    }
    _state["logs"].append(entry)
    # Keep only the most recent MAX_LOG_ENTRIES
    if len(_state["logs"]) > MAX_LOG_ENTRIES:
        _state["logs"] = _state["logs"][-MAX_LOG_ENTRIES:]
    _save_state(_state)


def make_bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return '\u2588' * filled + '\u2591' * (width - filled)


def _ytdlp_bin() -> str:
    return str(YTDLP_BIN) if YTDLP_BIN.exists() else "yt-dlp"


def _base_cmd() -> list:
    """Common yt-dlp flags shared by every invocation."""
    cmd = [
        _ytdlp_bin(),
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "--referer", "https://www.youtube.com/",
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    return cmd


async def fetch_video_info(url: str):
    """Run yt-dlp -j and return the parsed JSON dict, or None on failure."""
    cmd = _base_cmd() + ["-j", "--no-warnings", url]
    log = logging.getLogger("fetch_info")
    log.info("fetching info: %s", url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        log.error("yt-dlp -j timed out")
        return None
    if proc.returncode != 0:
        log.error("yt-dlp -j failed: %s", stderr.decode("utf-8", errors="replace"))
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("json parse error: %s", exc)
        return None


def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"~{mb / 1024:.1f} GB"
    return f"~{mb:.0f} MB"


def _estimate_size(fmt: dict, duration: float) -> int:
    """Return best-effort file size in bytes for a format dict.

    Prefers the exact ``filesize`` field, then ``filesize_approx``, and
    finally falls back to ``tbr`` (total bitrate in kbps) × duration.
    Returns 0 when no estimate is possible.
    """
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr")  # kilobits per second
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return 0


def build_quality_menu(info: dict) -> list:
    """
    Build a numbered list of download choices from yt-dlp video JSON.

    Rules:
    - One entry per distinct resolution (best stream at each target height).
    - Audio-only MP3 entry.
    - Subtitle entry when available.
    - Any option whose estimated combined size exceeds SIZE_LIMIT_BYTES is skipped.
    """
    formats = info.get("formats", [])
    duration = float(info.get("duration") or 0)

    video_fmts = [
        f for f in formats
        if f.get("vcodec", "none") != "none" and f.get("height")
    ]
    audio_fmts = [
        f for f in formats
        if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
    ]

    best_audio = (
        max(audio_fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        if audio_fmts else None
    )
    audio_size = (
        _estimate_size(best_audio, duration)
        if best_audio else 0
    )

    choices = []
    seen_heights = set()

    for target_h in [2160, 1440, 1080, 720, 480, 360, 240]:
        at_height = [f for f in video_fmts if f.get("height") == target_h]
        if not at_height:
            # Fall back to best available height strictly below this target
            below = [f for f in video_fmts if f.get("height", 0) < target_h]
            if not below:
                continue
            actual_h = max(f["height"] for f in below)
            if actual_h in seen_heights:
                continue
            at_height = [f for f in video_fmts if f["height"] == actual_h]
        else:
            actual_h = target_h

        if actual_h in seen_heights:
            continue
        seen_heights.add(actual_h)

        best_vid = max(at_height, key=lambda f: f.get("tbr") or f.get("vbr") or 0)
        vid_size = _estimate_size(best_vid, duration)
        total_size = vid_size + audio_size

        # Skip options that would exceed the 2 GB send limit
        if total_size > SIZE_LIMIT_BYTES:
            continue

        size_str = _fmt_size(total_size)
        label = "\U0001f3ac {}p".format(actual_h)
        if size_str:
            label += "  ({})".format(size_str)

        choices.append({
            "label": label,
            "format": (
                "bestvideo[height<={}]+bestaudio[ext=m4a]"
                "/bestvideo[height<={}]+bestaudio"
                "/best[height<={}]"
            ).format(actual_h, actual_h, actual_h),
            "audio_only": False,
            "subtitle_only": False,
            "out_ext": "mp4",
            "target_height": actual_h,
        })

    # Audio-only MP3
    if best_audio and audio_size <= SIZE_LIMIT_BYTES:
        audio_size_str = _fmt_size(audio_size)
        label = "\U0001f3b5 Audio only (MP3)"
        if audio_size_str:
            label += "  ({})".format(audio_size_str)
        choices.append({
            "label": label,
            "format": "bestaudio/best",
            "audio_only": True,
            "subtitle_only": False,
            "out_ext": "mp3",
        })

    # Subtitles (negligible size \u2014 always include when available)
    subtitles = info.get("subtitles", {})
    auto_captions = info.get("automatic_captions", {})
    all_langs = list(subtitles.keys()) + [
        la for la in auto_captions if la not in subtitles
    ]
    if all_langs:
        preferred = next((la for la in all_langs if la.startswith("en")), all_langs[0])
        auto_note = (
            " (auto-generated)"
            if preferred in auto_captions and preferred not in subtitles
            else ""
        )
        choices.append({
            "label": "\U0001f4c4 Subtitles \u2014 {}{}".format(preferred, auto_note),
            "format": None,
            "audio_only": False,
            "subtitle_only": True,
            "subtitle_lang": preferred,
            "out_ext": "srt",
        })

    return choices


def build_ytdlp_cmd_for_choice(url: str, choice: dict) -> list:
    """Build the full yt-dlp command for a specific quality choice."""
    cmd = _base_cmd()

    if choice.get("subtitle_only"):
        lang = choice.get("subtitle_lang", "en")
        cmd += [
            "--skip-download",
            "--write-sub", "--write-auto-sub",
            "--sub-lang", lang,
            "--convert-subs", "srt",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        ]
    elif choice.get("audio_only"):
        cmd += [
            "-f", choice["format"],
            "-x", "--audio-format", "mp3",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
            "--newline",
        ]
    else:
        cmd += [
            "-f", choice["format"],
            "--merge-output-format", "mp4",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
            "--newline",
        ]

    cmd.append(url)
    return cmd


app = RubikaClient(name=SESSION, display_welcome=True)


async def _notify_queue_positions() -> None:
    """Edit each waiting user's message to show their current queue position."""
    for pos, entry in enumerate(download_queue, 1):
        try:
            people = "person" if pos == 1 else "people"
            await app.edit_message(
                entry["object_guid"],
                entry["queue_msg_id"],
                "\u23f3 The bot is busy right now.\n"
                "There {} {} {} ahead of you.".format(
                    "is" if pos == 1 else "are", pos, people
                ),
            )
        except Exception:
            pass


async def _run_download_and_queue(
    object_guid: str, url: str, choice: dict, title: str
) -> None:
    """Run one download then hand off to the next entry in the queue."""
    global is_downloading
    log = logging.getLogger("queue")
    try:
        await _do_download(object_guid, url, choice, title, log)
    finally:
        if download_queue:
            next_entry = download_queue.popleft()
            # Refresh remaining users' position indicators
            await _notify_queue_positions()
            # Tell the next user they have reached the front
            try:
                await app.edit_message(
                    next_entry["object_guid"],
                    next_entry["queue_msg_id"],
                    "\U0001f7e2 You are the first person in the queue!\n"
                    "Starting your download\u2026",
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            # Dispatch next entry — may be Spotify or YouTube
            if next_entry["choice"] is None:
                asyncio.create_task(
                    _run_spotify_and_queue(next_entry["object_guid"], next_entry["url"])
                )
            else:
                asyncio.create_task(
                    _run_download_and_queue(
                        next_entry["object_guid"],
                        next_entry["url"],
                        next_entry["choice"],
                        next_entry["title"],
                    )
                )
        else:
            is_downloading = False


async def _expire_selection(object_guid: str) -> None:
    """Cancel a pending quality menu after SELECTION_TIMEOUT seconds."""
    await asyncio.sleep(SELECTION_TIMEOUT)
    entry = pending_selections.pop(object_guid, None)
    if entry:
        try:
            await app.send_message(
                object_guid,
                "\u23f0 Selection timed out. Send !download <url> to try again."
            )
        except Exception:
            pass


@app.on_message_updates(filters.commands("start", prefixes="!"))
async def start_handler(update):
    await app.send_message(
        update.object_guid,
        "\U0001f3ac Welcome!\n\n"
        "\U0001f4cc Commands:\n"
        "  !download <url> \u2014 Fetch quality options for a YouTube video\n"
        "  !spotify <url>  \u2014 Download a Spotify track\n"
        "  !tidal <url>    \u2014 Download a Tidal track\n"
        "  !qobuz <url>    \u2014 Download a Qobuz track\n"
        "  !amazon <url>   \u2014 Download an Amazon Music track\n"
        "  !cancel         \u2014 Cancel a pending quality selection\n"
        "  !start          \u2014 Show this message\n\n"
        "After sending !download the bot lists available qualities.\n"
        "Reply with !1, !2, \u2026 to pick one. Options above 2 GB are hidden.\n\n"
        "Music commands download as FLAC (Qobuz or Deezer, if credentials are\n"
        "configured) or MP3 320 k (YouTube Music fallback). Full metadata and\n"
        "cover art are embedded automatically."
    )


@app.on_message_updates(filters.commands("download", prefixes="!"))
async def download_handler(update):
    log = logging.getLogger("download")
    log.info("!download | text=%r | guid=%s", update.text, update.object_guid)

    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    match = YOUTUBE_RE.search(args)

    if not match:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid YouTube link.\n"
            "Example: !download https://youtu.be/abc123"
        )
        return

    url = match.group(0)
    _append_log(object_guid, "download_requested", url)

    # Cancel any existing pending selection for this chat
    old = pending_selections.pop(object_guid, None)
    if old and old.get("timeout_task"):
        old["timeout_task"].cancel()

    status = await app.send_message(object_guid, "\U0001f50d Fetching available qualities\u2026")
    status_id = status.message_id

    info = await fetch_video_info(url)
    if not info:
        await app.edit_message(
            object_guid, status_id,
            "\u274c Could not fetch video info. Check the URL and try again."
        )
        return

    choices = build_quality_menu(info)
    if not choices:
        await app.edit_message(
            object_guid, status_id,
            "\u274c No downloadable formats found under 2 GB."
        )
        return

    title = info.get("title", "")
    duration_s = info.get("duration")
    duration_str = ""
    if duration_s:
        m_val, s_val = divmod(int(duration_s), 60)
        h_val, m_val = divmod(m_val, 60)
        duration_str = "{}:{:02d}:{:02d}".format(h_val, m_val, s_val) if h_val else "{}:{:02d}".format(m_val, s_val)

    lines = []
    if title:
        lines.append("\U0001f3ac {}".format(title))
    if duration_str:
        lines.append("\u23f1 {}".format(duration_str))
    lines.append("")
    lines.append("Choose a quality (options above 2 GB are hidden):")
    for i, c in enumerate(choices, 1):
        lines.append("  !{} \u2014 {}".format(i, c["label"]))
    lines.append("  !cancel \u2014 Cancel")
    lines.append("")
    lines.append("\u23f0 This menu expires in 5 minutes.")

    await app.edit_message(object_guid, status_id, "\n".join(lines))

    timeout_task = asyncio.create_task(_expire_selection(object_guid))
    pending_selections[object_guid] = {
        "url": url,
        "choices": choices,
        "title": title,
        "timeout_task": timeout_task,
    }
    log.info("menu sent | %d choices | guid=%s", len(choices), object_guid)


@app.on_message_updates(
    filters.commands(["1", "2", "3", "4", "5", "6", "7", "8", "9", "cancel"], prefixes="!")
)
async def selection_handler(update):
    global is_downloading
    log = logging.getLogger("selection")
    object_guid = update.object_guid
    cmd_name = update.command[0] if update.command else ""
    log.info("!%s | guid=%s", cmd_name, object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    if cmd_name == "cancel":
        entry = pending_selections.pop(object_guid, None)
        if entry and entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        # Also remove from download queue if the user is waiting there
        queue_before = len(download_queue)
        new_queue = collections.deque(
            e for e in download_queue if e["object_guid"] != object_guid
        )
        download_queue.clear()
        download_queue.extend(new_queue)
        queue_removed = len(download_queue) < queue_before
        if queue_removed:
            asyncio.create_task(_notify_queue_positions())

        msg = (
            "\u274c Download cancelled."
            if (entry or queue_removed)
            else "\u2139\ufe0f No active download to cancel."
        )
        await app.send_message(object_guid, msg)
        return

    entry = pending_selections.get(object_guid)
    if not entry:
        await app.send_message(
            object_guid,
            "\u26a0\ufe0f No active quality menu. Use !download <url> first."
        )
        return

    try:
        idx = int(cmd_name) - 1
    except ValueError:
        await app.send_message(object_guid, "\u274c Invalid selection.")
        return

    choices = entry["choices"]
    if idx < 0 or idx >= len(choices):
        await app.send_message(
            object_guid,
            "\u274c Please choose between !1 and !{}, or !cancel.".format(len(choices))
        )
        return

    # Consume the entry and cancel its expiry timer before starting download
    pending_selections.pop(object_guid, None)
    if entry.get("timeout_task"):
        entry["timeout_task"].cancel()

    choice = choices[idx]
    url = entry["url"]
    title = entry.get("title", "")
    _append_log(object_guid, "download_started", "{} | {}".format(choice["label"], url))

    # -- Queue logic ----------------------------------------------------------
    # Check and set is_downloading atomically (no await in between -- safe in asyncio).
    if not is_downloading:
        is_downloading = True
        asyncio.create_task(_run_download_and_queue(object_guid, url, choice, title))
    else:
        pos = len(download_queue) + 1
        people = "person" if pos == 1 else "people"
        queue_msg = await app.send_message(
            object_guid,
            "\u23f3 The bot is busy right now.\n"
            "There {} {} {} ahead of you.".format(
                "is" if pos == 1 else "are", pos, people
            ),
        )
        download_queue.append({
            "object_guid": object_guid,
            "url": url,
            "choice": choice,
            "title": title,
            "queue_msg_id": queue_msg.message_id,
        })
        log.info("queued at position %d | guid=%s", pos, object_guid)



async def _do_download(object_guid: str, url: str, choice: dict, title: str, log) -> None:
    """Run yt-dlp for the selected choice, stream progress, then send the file."""
    status = await app.send_message(object_guid, "\u23f3 Starting: {}\u2026".format(choice["label"]))
    status_id = status.message_id

    cmd = build_ytdlp_cmd_for_choice(url, choice)
    log.info("cmd: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log.info("yt-dlp pid=%s", proc.pid)

        downloaded_file = None
        last_update = 0.0
        out_ext = choice.get("out_ext", "mp4")

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log.debug("yt-dlp | %s", line)

            # Capture file path printed by --print after_move:filepath
            if line and not line.startswith("[") and line.endswith(".{}".format(out_ext)):
                downloaded_file = line
                log.info("filepath: %s", downloaded_file)
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
                        "\U0001f4e5 {}\n[{}] {:.1f}%\n\u26a1 {}".format(
                            choice["label"], bar, percent, speed
                        )
                    )
                except Exception as exc:
                    log.warning("edit failed: %s", exc)

        await proc.wait()
        log.info("returncode=%s", proc.returncode)

        if proc.returncode != 0:
            await app.edit_message(object_guid, status_id, "\u274c Download failed. Please try again.")
            return

        # Subtitle-only: find the newest .srt written by yt-dlp
        if choice.get("subtitle_only"):
            srts = list(DOWNLOAD_DIR.glob("*.srt"))
            if srts:
                downloaded_file = str(max(srts, key=lambda p: p.stat().st_mtime))

        # Fallback: glob for the expected file extension
        if not downloaded_file:
            candidates = list(DOWNLOAD_DIR.glob("*.{}".format(out_ext)))
            if candidates:
                downloaded_file = str(max(candidates, key=lambda p: p.stat().st_mtime))

        if not downloaded_file or not Path(downloaded_file).exists():
            await app.edit_message(object_guid, status_id, "\u274c Downloaded file not found.")
            return

        file_path = Path(downloaded_file)
        size_mb = file_path.stat().st_size / (1024 * 1024)
        log.info("sending: %s (%.2f MB)", downloaded_file, size_mb)

        await app.edit_message(object_guid, status_id, "\u2705 Download complete. Sending\u2026")
        caption = "{}\n{}".format(title, url) if title else url
        await app.send_document(object_guid, downloaded_file, caption=caption)
        log.info("sent successfully")

        try:
            file_path.unlink()
            log.info("removed: %s", downloaded_file)
        except Exception as exc:
            log.warning("cleanup failed: %s", exc)

        await app.edit_message(object_guid, status_id, "\u2705 Done! File sent.")

    except Exception as exc:
        log.exception("error in _do_download: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass



@app.on_message_updates(filters.commands("admin", prefixes="!"))
async def admin_handler(update):
    """
    Admin command handler.  Only accounts listed in ADMIN_GUIDS may use this.

    Sub-commands
    ------------
    !admin whitelist add <guid>    – add a user to the whitelist
    !admin whitelist remove <guid> – remove a user from the whitelist
    !admin whitelist on            – enable whitelist mode (only listed users can use bot)
    !admin whitelist off           – disable whitelist mode (open to everyone)
    !admin ban <guid>              – ban a user
    !admin unban <guid>            – unban a user
    !admin logs [N]                – show last N (default 20) usage log entries
    !admin status                  – show current settings summary
    """
    object_guid = update.object_guid
    log = logging.getLogger("admin")

    if not _is_admin(object_guid):
        await app.send_message(object_guid, "🚫 You do not have admin privileges.")
        return

    parts = update.command  # ['admin', 'sub', ...]
    if len(parts) < 2:
        await app.send_message(
            object_guid,
            "⚙️ Admin commands:\n"
            "  !admin whitelist add <guid>\n"
            "  !admin whitelist remove <guid>\n"
            "  !admin whitelist on\n"
            "  !admin whitelist off\n"
            "  !admin ban <guid>\n"
            "  !admin unban <guid>\n"
            "  !admin logs [N]\n"
            "  !admin status"
        )
        return

    sub = parts[1].lower()

    # ------------------------------------------------------------------
    # whitelist sub-commands
    # ------------------------------------------------------------------
    if sub == "whitelist":
        if len(parts) < 3:
            await app.send_message(
                object_guid,
                "Usage: !admin whitelist add|remove|on|off [<guid>]"
            )
            return
        action = parts[2].lower()

        if action in ("on", "off"):
            _state["whitelist_enabled"] = (action == "on")
            _save_state(_state)
            status_word = "enabled" if _state["whitelist_enabled"] else "disabled"
            log.info("whitelist %s by %s", status_word, object_guid)
            await app.send_message(
                object_guid,
                "✅ Whitelist mode {}.".format(status_word)
            )
            return

        if action in ("add", "remove"):
            if len(parts) < 4:
                await app.send_message(
                    object_guid,
                    "Usage: !admin whitelist {} <guid>".format(action)
                )
                return
            target = parts[3].strip()
            wl = _state["whitelist"]
            if action == "add":
                if target not in wl:
                    wl.append(target)
                    _save_state(_state)
                    log.info("whitelist add %s by %s", target, object_guid)
                await app.send_message(
                    object_guid,
                    "✅ {} added to whitelist.".format(target)
                )
            else:  # remove
                if target in wl:
                    wl.remove(target)
                    _save_state(_state)
                    log.info("whitelist remove %s by %s", target, object_guid)
                    await app.send_message(
                        object_guid,
                        "✅ {} removed from whitelist.".format(target)
                    )
                else:
                    await app.send_message(
                        object_guid,
                        "ℹ️ {} was not in the whitelist.".format(target)
                    )
            return

        await app.send_message(
            object_guid,
            "❌ Unknown whitelist action. Use add / remove / on / off."
        )
        return

    # ------------------------------------------------------------------
    # ban / unban
    # ------------------------------------------------------------------
    if sub in ("ban", "unban"):
        if len(parts) < 3:
            await app.send_message(
                object_guid,
                "Usage: !admin {} <guid>".format(sub)
            )
            return
        target = parts[2].strip()
        banned = _state["banned"]
        if sub == "ban":
            if target not in banned:
                banned.append(target)
                _save_state(_state)
                log.info("banned %s by %s", target, object_guid)
            await app.send_message(
                object_guid,
                "🚫 {} has been banned.".format(target)
            )
        else:  # unban
            if target in banned:
                banned.remove(target)
                _save_state(_state)
                log.info("unbanned %s by %s", target, object_guid)
                await app.send_message(
                    object_guid,
                    "✅ {} has been unbanned.".format(target)
                )
            else:
                await app.send_message(
                    object_guid,
                    "ℹ️ {} was not banned.".format(target)
                )
        return

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------
    if sub == "logs":
        try:
            n = int(parts[2]) if len(parts) >= 3 else 20
        except ValueError:
            n = 20
        n = max(1, min(n, 100))
        entries = _state["logs"][-n:]
        if not entries:
            await app.send_message(object_guid, "ℹ️ No usage logs yet.")
            return
        lines = ["📋 Last {} usage log entries:".format(len(entries))]
        for e in reversed(entries):
            lines.append(
                "[{}] {} | {} | {}".format(
                    e.get("time", "?"),
                    e.get("guid", "?"),
                    e.get("action", "?"),
                    e.get("detail", ""),
                )
            )
        # Split into chunks of ≤4000 chars to avoid message size limits
        chunk, chunks = [], []
        for line in lines:
            if sum(len(l) + 1 for l in chunk) + len(line) > 3800:
                chunks.append("\n".join(chunk))
                chunk = []
            chunk.append(line)
        if chunk:
            chunks.append("\n".join(chunk))
        for c in chunks:
            await app.send_message(object_guid, c)
        return

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    if sub == "status":
        wl_mode = "ON" if _state["whitelist_enabled"] else "OFF"
        wl_count = len(_state["whitelist"])
        ban_count = len(_state["banned"])
        log_count = len(_state["logs"])
        admin_count = len(ADMIN_GUIDS)
        await app.send_message(
            object_guid,
            "⚙️ Bot status:\n"
            "  Admins configured : {}\n"
            "  Whitelist mode    : {}\n"
            "  Whitelisted users : {}\n"
            "  Banned users      : {}\n"
            "  Log entries       : {}".format(
                admin_count, wl_mode, wl_count, ban_count, log_count
            )
        )
        return

    await app.send_message(
        object_guid,
        "❌ Unknown admin sub-command: {}".format(sub)
    )


# ---------------------------------------------------------------------------
# Generic music downloading  (SpotiFLAC approach via spotify_dl.py)
# ---------------------------------------------------------------------------

_PLATFORM_NAMES = {
    "spotify": "Spotify",
    "tidal":   "Tidal",
    "qobuz":   "Qobuz",
    "amazon":  "Amazon Music",
}


async def _do_music_download(object_guid: str, url: str, platform: str, log) -> None:
    """Resolve metadata for *url* from *platform*, pick the best download source, send file."""
    pname = _PLATFORM_NAMES.get(platform, platform.capitalize())
    status = await app.send_message(object_guid, "\U0001f50d Looking up {} track\u2026".format(pname))
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()

        # --- Fetch metadata ---
        if platform == "spotify":
            track_id = _spodl.parse_spotify_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Spotify track ID from: {}".format(url))
                return
            try:
                await app.edit_message(object_guid, status_id, "\U0001f4e1 Fetching Spotify metadata\u2026")
            except Exception:
                pass
            info = await loop.run_in_executor(None, _spodl.get_track_info, track_id)

        elif platform == "tidal":
            track_id = _spodl.parse_tidal_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Tidal track ID from: {}".format(url))
                return
            try:
                await app.edit_message(object_guid, status_id, "\U0001f4e1 Fetching Tidal metadata\u2026")
            except Exception:
                pass
            info = await loop.run_in_executor(None, _spodl.get_tidal_track_info, track_id)

        elif platform == "qobuz":
            track_id = _spodl.parse_qobuz_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Qobuz track ID from: {}".format(url))
                return
            try:
                await app.edit_message(object_guid, status_id, "\U0001f4e1 Fetching Qobuz metadata\u2026")
            except Exception:
                pass
            info = await loop.run_in_executor(None, _spodl.get_qobuz_track_info, track_id)

        elif platform == "amazon":
            track_id = _spodl.parse_amazon_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Amazon Music track ID from: {}".format(url))
                return
            try:
                await app.edit_message(object_guid, status_id, "\U0001f4e1 Fetching Amazon Music metadata\u2026")
            except Exception:
                pass
            ytbin = _ytdlp_bin()
            info = await loop.run_in_executor(None, _spodl.get_amazon_track_info, track_id, ytbin)

        else:
            await app.edit_message(object_guid, status_id, "\u274c Unknown platform: {}".format(platform))
            return

        title       = info.get("title") or "Unknown"
        artists_str = ", ".join(info.get("artists") or [])
        source_label = _spodl.best_source_label(info)

        try:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e5 Downloading [{}]: {} \u2014 {}".format(source_label, title, artists_str),
            )
        except Exception:
            pass

        # --- Download + tag ---
        fp = await _spodl.download_track(info, DOWNLOAD_DIR, _ytdlp_bin())

        try:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e4 Sending {} ({})…".format(fp.name, fp.suffix.lstrip(".").upper()),
            )
        except Exception:
            pass

        caption = "{} — {}".format(title, artists_str) if artists_str else title
        try:
            await app.send_document(object_guid, str(fp), caption=caption)
            log.info("sent: %s", fp)
        except Exception as exc:
            log.error("send failed: %s", exc)
            await app.send_message(object_guid, "\u274c Failed to send file: {}".format(exc))
        finally:
            try:
                fp.unlink()
            except Exception:
                pass

        await app.edit_message(object_guid, status_id, "\u2705 Done!")

    except Exception as exc:
        log.exception("_do_music_download (%s) error: %s", platform, exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass


# Keep the old name as an alias so queue code can call it unchanged
async def _do_spotify_download(object_guid: str, url: str, log) -> None:
    await _do_music_download(object_guid, url, "spotify", log)


@app.on_message_updates(filters.commands("spotify", prefixes="!"))
async def spotify_handler(update):
    global is_downloading
    log = logging.getLogger("spotify")
    object_guid = update.object_guid
    log.info("!spotify | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    track_id = _spodl.parse_spotify_track_id(args)

    if not track_id:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Spotify track link.\n"
            "Accepted formats:\n"
            "  https://open.spotify.com/track/<id>\n"
            "  spotify:track:<id>\n"
            "  (bare 22-character track ID)"
        )
        return

    url = args.strip()
    _append_log(object_guid, "spotify_track", url)
    await _enqueue_music(object_guid, url, "spotify", log)


@app.on_message_updates(filters.commands("tidal", prefixes="!"))
async def tidal_handler(update):
    global is_downloading
    log = logging.getLogger("tidal")
    object_guid = update.object_guid
    log.info("!tidal | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = TIDAL_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Tidal track link.\n"
            "Example: https://tidal.com/browse/track/12345678"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "tidal_track", url)
    await _enqueue_music(object_guid, url, "tidal", log)


@app.on_message_updates(filters.commands("qobuz", prefixes="!"))
async def qobuz_handler(update):
    global is_downloading
    log = logging.getLogger("qobuz")
    object_guid = update.object_guid
    log.info("!qobuz | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = QOBUZ_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Qobuz track link.\n"
            "Example: https://open.qobuz.com/track/12345678"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "qobuz_track", url)
    await _enqueue_music(object_guid, url, "qobuz", log)


@app.on_message_updates(filters.commands("amazon", prefixes="!"))
async def amazon_handler(update):
    global is_downloading
    log = logging.getLogger("amazon")
    object_guid = update.object_guid
    log.info("!amazon | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = AMAZON_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Amazon Music track link.\n"
            "Example: https://music.amazon.com/tracks/B01N2XLBTT"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "amazon_track", url)
    await _enqueue_music(object_guid, url, "amazon", log)


async def _enqueue_music(object_guid: str, url: str, platform: str, log) -> None:
    """Add a music download to the queue or start it immediately."""
    global is_downloading
    if not is_downloading:
        is_downloading = True
        asyncio.create_task(_run_music_and_queue(object_guid, url, platform))
    else:
        pos = len(download_queue) + 1
        people = "person" if pos == 1 else "people"
        queue_msg = await app.send_message(
            object_guid,
            "\u23f3 The bot is busy right now.\n"
            "There {} {} {} ahead of you.".format(
                "is" if pos == 1 else "are", pos, people
            ),
        )
        download_queue.append({
            "object_guid":  object_guid,
            "url":          url,
            "choice":       None,       # sentinel: music-platform entry (not YouTube)
            "platform":     platform,
            "title":        url,
            "queue_msg_id": queue_msg.message_id,
        })
        log.info("%s queued at position %d | guid=%s", platform, pos, object_guid)


async def _run_music_and_queue(object_guid: str, url: str, platform: str) -> None:
    """Run one music download then hand off to the next queue entry."""
    global is_downloading
    log = logging.getLogger("queue")
    try:
        await _do_music_download(object_guid, url, platform, log)
    finally:
        if download_queue:
            next_entry = download_queue.popleft()
            await _notify_queue_positions()
            try:
                await app.edit_message(
                    next_entry["object_guid"],
                    next_entry["queue_msg_id"],
                    "\U0001f7e2 You are the first person in the queue!\n"
                    "Starting your download\u2026",
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            # Dispatch the next entry — music platform or YouTube quality
            if next_entry["choice"] is None:
                next_platform = next_entry.get("platform", "spotify")
                asyncio.create_task(
                    _run_music_and_queue(
                        next_entry["object_guid"],
                        next_entry["url"],
                        next_platform,
                    )
                )
            else:
                asyncio.create_task(
                    _run_download_and_queue(
                        next_entry["object_guid"],
                        next_entry["url"],
                        next_entry["choice"],
                        next_entry["title"],
                    )
                )
        else:
            is_downloading = False


if __name__ == "__main__":
    print("[rub] Connecting to Rubika...")
    try:
        app.run(phone_number=PHONE_NUMBER)
    except Exception as exc:
        print("[rub] Connection failed: {}".format(exc))
        raise
    print("[rub] Disconnected.")
