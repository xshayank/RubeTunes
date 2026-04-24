<div align="center">

<img src="https://img.shields.io/badge/RubeTunes-Music%20%26%20Video%20Bot-blueviolet?style=for-the-badge&logo=music&logoColor=white" alt="RubeTunes"/>

# 🎵 RubeTunes

**A powerful Rubika bot that downloads music & videos from the web and sends them straight to your chat.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)](https://python.org)
[![Rubika](https://img.shields.io/badge/Platform-Rubika-orange?style=flat-square)](https://rubika.ir)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Tests](https://github.com/xshayank/RubeTunes/actions/workflows/tests.yml/badge.svg)](https://github.com/xshayank/RubeTunes/actions/workflows/tests.yml)

</div>

---

## 🌟 What is RubeTunes?

**RubeTunes** is a self-hosted Rubika bot that lets you send any YouTube, Spotify, Qobuz, Tidal, or Amazon Music link and receive the downloaded file — video or audio — directly in your Rubika chat.

- Send a **YouTube link** → pick your preferred quality → get the file
- Send a **Spotify / Qobuz / Tidal / Amazon Music link** → get lossless FLAC or MP3 audio
- Everything runs through a **queue** so concurrent requests never conflict

---

## ✨ Features

| Category | Details |
|---|---|
| 📺 **YouTube & YouTube Music** | Download videos up to **4K** or audio-only as **MP3** |
| 🎵 **Spotify** | Single tracks, full playlists & albums |
| 🎶 **Qobuz** | Hi-Res & lossless **FLAC** (no account needed) |
| 🌊 **Tidal** | Track metadata + ISRC-based download |
| 🎙 **Amazon Music** | Track download via ISRC resolution |
| 📄 **Subtitles** | Auto-detected subtitles in SRT format |
| ⚡ **Download Queue** | One download at a time — crash-proof & conflict-free |
| 🔀 **Concurrent Batch** | Albums & playlists download up to 3 tracks in parallel |
| 🛡️ **Circuit Breaker** | Failing providers are temporarily disabled automatically |
| 🕘 **Download History** | `!history` shows your recent downloads |
| 🔒 **Admin Controls** | Whitelist mode, per-user bans, usage logs, cache management |
| 💾 **2 GB file limit** | Files up to **2 GB** sent natively via Rubika |

---

## ⚙️ How It Works

```
User sends link in Rubika
        ↓
Bot detects link type (YouTube / Spotify / Qobuz / …)
        ↓
Bot presents quality / format options (inline keyboard)
        ↓
User picks a quality
        ↓
Request joins the download queue
        ↓
yt-dlp / spotify_dl fetches & processes the file
        ↓
File is sent back to the user in Rubika
```

### Music quality resolution chain

For music links the bot tries sources in this order until one succeeds:

1. **Qobuz FLAC Hi-Res** (27-bit) — *no account required*
2. **Qobuz FLAC CD** (16-bit)
3. **Deezer FLAC** — requires `DEEZER_ARL` cookie
4. **YouTube Music MP3** — always available as a fallback

---

## 🚀 Recent improvements

The following features were ported from the [SpotiFLAC](https://github.com/spotbye/SpotiFLAC) Go backend:

| # | Feature | SpotiFLAC reference |
|---|---|---|
| 1 | **Amazon Music decryption** — `ffmpeg -decryption_key` applied before FLAC conversion when the proxy returns a key | `backend/amazon.go` |
| 2 | **Tidal V2 manifest downloads** — base64-decoded multi-segment manifest responses are fetched and concatenated | `backend/tidal.go` `DownloadFromManifest` |
| 3 | **Parallel platform resolution** — Deezer, Qobuz, Tidal, Tidal Alt, and Odesli lookups now run concurrently (saves 5–10 s per track) | `backend/analysis.go` `CheckTrackAvailability` |
| 4 | **M4A metadata tagging** — `.m4a` files are now tagged via `mutagen.mp4` (ISRC, cover art, lyrics) with an ffmpeg remux fallback | `backend/metadata.go` `EmbedMetadata` |
| 5 | **Tidal endpoint rotation** — multiple proxy base URLs are tried in order; configurable via `TIDAL_ALT_BASES` env var | `backend/tidal_api_list.go` |
| 6 | **In-process metadata cache** — track info is cached for 10 min (LRU, 256 entries) to avoid redundant API calls | `backend/recent_fetches.go` |
| 7 | **Download history** — successful downloads are recorded in `downloads_history.json`; repeat requests reuse the existing file | `backend/history.go` |
| 8 | **Authenticated Qobuz fallback** — optional `QOBUZ_EMAIL` / `QOBUZ_PASSWORD` env vars enable a signed `track/getFileUrl` call when all proxy APIs fail | `backend/qobuz_api.go` `userLogin` |

And the following UX & resilience improvements:

| # | Feature |
|---|---|
| 9 | **`!history` command** — users see their own recent downloads; admins can view global history |
| 10 | **`!admin clearcache`** — flush the in-memory LRU and/or ISRC disk cache on demand |
| 11 | **`!admin breakers`** — inspect provider circuit-breaker states in real time |
| 12 | **Concurrent batch downloads** — albums/playlists download up to 3 tracks in parallel (configurable) |
| 13 | **Provider circuit breaker** — providers that fail repeatedly are automatically skipped until they recover |
| 14 | **Resolver unit tests** — `pytest`-based test suite covering parsers, resolvers, circuit breaker, and LRU cache |

---

## 🚀 Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/xshayank/RubeTunes.git
cd RubeTunes

# 2. Create & activate a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure your environment
cp .env.example .env
nano .env   # fill in your credentials (see below)

# 5. Run the bot
python3 main.py
```

> **First run only:** the bot will prompt you to enter your Rubika phone number and the verification code. The session is then saved locally — you won't be asked again.

---

## 🔧 Configuration

All settings live in a `.env` file in the project root.

```env
# ── Rubika ──────────────────────────────────────────────────────────────────
RUBIKA_SESSION=rubika_session        # local session file name (no extension)
RUBIKA_PHONE=09xxxxxxxxx             # your Rubika phone number

# ── Admin ───────────────────────────────────────────────────────────────────
# Comma-separated Rubika object GUIDs that have admin privileges
ADMIN_GUIDS=

# ── Spotify (optional) ───────────────────────────────────────────────────────
# Anonymous Spotify token is obtained automatically — no account needed.
# Only required if the anonymous token ever fails.
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=

# ── Deezer (lossless FLAC) ───────────────────────────────────────────────────
# Obtain your ARL from browser cookies after logging in to deezer.com.
# Leave blank to skip Deezer and use Qobuz or YouTube Music instead.
DEEZER_ARL=

# ── Qobuz ────────────────────────────────────────────────────────────────────
# Credentials are auto-scraped — no account, email, or API key needed.
# Optional: set both of the following for an authenticated fallback when all
# proxy APIs fail (port of SpotiFLAC backend/qobuz_api.go userLogin).
QOBUZ_EMAIL=
QOBUZ_PASSWORD=

# ── Tidal (metadata only) ────────────────────────────────────────────────────
TIDAL_TOKEN=

# ── Tidal Alt proxy base URLs (optional) ─────────────────────────────────────
# Comma-separated list of base URLs for the Tidal Alt proxy.
# Defaults to the built-in list if not set.
# (port of SpotiFLAC backend/tidal_api_list.go)
TIDAL_ALT_BASES=

# ── Batch download concurrency ────────────────────────────────────────────────
# Number of tracks downloaded in parallel for albums/playlists.
# Default: 3  |  Min: 1  |  Max: 6
BATCH_CONCURRENCY=3

# ── Provider circuit breaker ──────────────────────────────────────────────────
# Open the circuit after N consecutive failures within W seconds.
# Keep it open for T seconds, then allow one probe (half-open state).
CIRCUIT_FAIL_THRESHOLD=3
CIRCUIT_FAIL_WINDOW_SEC=300
CIRCUIT_OPEN_DURATION_SEC=600
```

---

## 🖥️ Server Setup (Production)

```bash
# Install system dependencies
sudo apt update
sudo apt install python3 python3-venv python3-pip git -y

# Clone and enter the project
git clone https://github.com/xshayank/RubeTunes.git
cd RubeTunes

# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env

# Run persistently with screen
screen -S rubetunes
source venv/bin/activate
python3 main.py
# Detach: Ctrl+A then D
# Reattach later: screen -r rubetunes
```

---

## 📋 User Commands

| Command | Description |
|---|---|
| `!start` | Show the welcome / help message |
| `!download <url>` | Download a YouTube video (choose quality) |
| `!spotify <url>` | Download a Spotify track / album / playlist, or browse an artist page |
| `!tidal <url>` | Download a Tidal track |
| `!qobuz <url>` | Download a Qobuz track |
| `!amazon <url>` | Download an Amazon Music track |
| `!cancel` | Cancel a pending quality-selection menu |
| `!history [N]` | Show your N most recent downloads (default 10, max 25) |

---

## 👑 Admin Commands

Send these commands in your Rubika chat with the bot (admin only — set via `ADMIN_GUIDS` env var):

| Command | Description |
|---|---|
| `!admin whitelist on` | Enable whitelist mode — only approved users can use the bot |
| `!admin whitelist off` | Disable whitelist mode |
| `!admin whitelist add <guid>` | Add a user to the whitelist |
| `!admin whitelist remove <guid>` | Remove a user from the whitelist |
| `!admin ban <guid>` | Permanently ban a user |
| `!admin unban <guid>` | Remove a user's ban |
| `!admin logs [N]` | View last N usage log entries (default 20) |
| `!admin status` | Show current bot settings summary |
| `!admin clearcache [lru\|isrc\|all]` | Flush in-memory LRU cache and/or ISRC disk cache |
| `!admin breakers` | Show current circuit-breaker state for every provider |
| `!history all` | View global recent download history (admin only) |

---

## 🧪 Running Tests

```bash
# Install dev dependencies (adds pytest + responses)
pip install -r requirements-dev.txt

# Run the test suite
pytest -q tests/
```

Tests run fully **offline** — all HTTP calls are mocked via the `responses` library.
CI runs automatically on every push and pull request via GitHub Actions (`.github/workflows/tests.yml`).

---

## 🛠️ Tech Stack

| Component | Library |
|---|---|
| Rubika client | [`rubpy`](https://github.com/shayanheidari01/rubpy) |
| YouTube / video download | [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) |
| Audio tagging | [`mutagen`](https://github.com/quodlibet/mutagen) |
| Spotify metadata | Internal GraphQL persisted-query client |
| Qobuz metadata | Auto-scraped credentials from `open.qobuz.com` |
| Environment config | [`python-dotenv`](https://github.com/theskumar/python-dotenv) |
| Testing | [`pytest`](https://pytest.org) + [`responses`](https://github.com/getsentry/responses) |

---

## 📋 Requirements

- Python **3.10+**
- A Rubika account
- A server or always-on machine to host the bot
- *(Optional)* Deezer ARL for lossless FLAC downloads

---

<div align="center">

Made with ❤️ for the Rubika community

</div>
