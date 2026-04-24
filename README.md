<div align="center">

<img src="https://img.shields.io/badge/RubeTunes-Music%20%26%20Video%20Bot-blueviolet?style=for-the-badge&logo=music&logoColor=white" alt="RubeTunes"/>

# 🎵 RubeTunes

**A powerful Rubika bot that downloads music & videos from the web and sends them straight to your chat.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)](https://python.org)
[![Rubika](https://img.shields.io/badge/Platform-Rubika-orange?style=flat-square)](https://rubika.ir)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

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
| 🔒 **Admin Controls** | Whitelist mode, per-user bans, usage logs |
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

# ── Tidal (metadata only) ────────────────────────────────────────────────────
TIDAL_TOKEN=
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

## 👑 Admin Commands

Send these commands in your Rubika chat with the bot (admin only):

| Command | Description |
|---|---|
| `/whitelist on` | Enable whitelist mode — only approved users can use the bot |
| `/whitelist off` | Disable whitelist mode |
| `/whitelist add <guid>` | Add a user to the whitelist |
| `/whitelist remove <guid>` | Remove a user from the whitelist |
| `/ban <guid>` | Permanently ban a user |
| `/unban <guid>` | Remove a user's ban |
| `/logs` | View recent usage logs |

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
