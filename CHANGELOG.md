# Changelog

All notable changes to RubeTunes are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] — Mega Improvement PR

### Added — Priority A: Foundation

- **A1 Package refactor**: Split `spotify_dl.py` (4543 lines) into `rubetunes/` package:
  - `rubetunes/cache.py` — LRU track-info cache + ISRC disk cache
  - `rubetunes/circuit_breaker.py` — provider circuit-breaker state machine
  - `rubetunes/history.py` — download history helpers
  - `rubetunes/tagging.py` — `embed_metadata` (MP3/FLAC/M4A)
  - `rubetunes/spotify_meta.py` — Spotify TOTP, tokens, GraphQL, SpotifyClient, ISRC
  - `rubetunes/providers/` — per-provider modules (Qobuz, Tidal, Tidal Alt, Deezer, Amazon)
  - `rubetunes/resolver.py` — multi-platform resolver (Odesli, Songstats, MusicBrainz)
  - `rubetunes/downloader.py` — quality constants + download orchestration
  - `spotify_dl.py` kept as thin compatibility shim (zero changes to `rub.py` imports)
- **A2 Type hints + mypy**: `mypy.ini` added; all public functions annotated; non-blocking mypy step in CI
- **A3 Pre-commit + linting**: `.pre-commit-config.yaml` with black, ruff, end-of-file-fixer, trailing-whitespace; `pyproject.toml` with black line-length 100 and ruff E/F/I/UP/B rules
- **A4 Docker + Compose**: `Dockerfile` (python:3.11-slim + ffmpeg + yt-dlp), `docker-compose.yml` with named volumes, port 9090 for metrics, `.dockerignore`
- **A5 Structured JSON logging**: `rubetunes/logging_setup.py` with `setup_logging()`; toggle via `LOG_FORMAT=json`; python-json-logger optional dependency
- **A6 Prometheus metrics**: `rubetunes/metrics.py`; counters `rubetunes_downloads_total`, `rubetunes_provider_failures_total`, `rubetunes_resolutions_total`; gauges `rubetunes_queue_depth`, `rubetunes_circuit_open`; histograms for duration; served on `METRICS_PORT` (default 9090)
- **A7 Sentry**: `rubetunes/sentry_setup.py`; init only if `SENTRY_DSN` set; captures user GUID + command tag
- **A8 Graceful shutdown**: `main.py` traps SIGTERM/SIGINT; waits up to `SHUTDOWN_TIMEOUT_SEC` for in-flight downloads; restores `queue_snapshot.json` on startup

### Added — Priority B: User-visible features

- **B1 `!search <query>`**: Spotify search via public REST API; top 10 results as numbered menu; user replies `!1`–`!10` to download
- **B3 `!queue`**: Shows user's position in queue + items ahead
- **B6 Format hint**: Append `mp3`/`flac`/`m4a` to music commands; documented in `!start`

### Added — Priority C: New providers

- **C1 SoundCloud provider**: `!soundcloud <url>` via yt-dlp; `rubetunes/providers/soundcloud.py`
- **C2 Bandcamp provider**: `!bandcamp <url>` via yt-dlp (FLAC preferred); `rubetunes/providers/bandcamp.py`
- **C4 Apple Music metadata enrichment**: `rubetunes/providers/apple_music.py`; iTunes Search API for high-res cover art (1400×1400) and track/disc numbers

### Added — Priority D: Operations & abuse prevention

- **D1 `!admin health`**: Concurrent HEAD pings of Qobuz, Deezer, Tidal, lrclib, MusicBrainz, Odesli, YouTube Music; reports up/down/slow (>2s)
- **D2 Disk space guard**: `rubetunes/disk_guard.py`; checks free space before batch download; rejects if < 2× estimated (configurable via `MIN_FREE_SPACE_MB`)
- **D3 Per-user rate limiting**: `rubetunes/rate_limiter.py`; rolling 1-hour window; default 100 tracks/hour (`USER_TRACKS_PER_HOUR`); cooldown ETA in rejection message

### Added — Priority E: Versioning

- **E1 Versioned releases**: `rubetunes/__init__.py` exports `__version__ = "2.0.0"`; this `CHANGELOG.md`; GitHub Actions release workflow `.github/workflows/release.yml` (tag `v*.*.*` → Docker image pushed to GHCR + GitHub Release)

### Changed

- `main.py` overhauled: logging, Sentry, Prometheus initialisation at startup; graceful SIGTERM/SIGINT shutdown
- `!start` help message updated with new commands
- `requirements.txt` adds `python-json-logger`, `prometheus-client`, `sentry-sdk`
- `requirements-dev.txt` adds `pre-commit`, `black`, `ruff`, `mypy`

### TODO (not yet implemented)

- **B4 `!favorite`** — per-user favorites file
- **B5 Inline progress bars** — `▰▰▰▱▱▱ 50% · 2.3 MB/s`
- **B7 Loudness normalization** — `--normalize` flag, ffmpeg loudnorm two-pass
- **B8 Auto-remember quality** — per-user preference file
- **B9 ReplayGain tags** — r128gain integration
- **B10 Album art upscale** — Apple Music cover fallback already implemented (C4)
- **C3 YouTube Music ISRC search** — ytmusicapi ISRC-first search
- **C5 Spotify in-app lyrics** — `/color-lyrics/v2/track/{id}` endpoint
- **C6 Batch dedup** — check history across simultaneous duplicate playlists
- **D4 Per-source rate limiting** — token-bucket per provider
- **D5 Audit log redaction** — redact full URLs for banned users

---

## [1.1.0] — UX & Resilience PR

### Added

- `!history` command — users see their own recent downloads; admins see global history
- `!admin clearcache` — flush LRU and/or ISRC disk cache on demand
- `!admin breakers` — inspect provider circuit-breaker states
- Concurrent batch downloads (albums/playlists, up to 3 in parallel, configurable)
- Provider circuit breaker — auto-skip failing providers
- `pytest` test suite covering parsers, resolvers, circuit breaker, LRU cache

---

## [1.0.0] — SpotiFLAC Parity PR

### Added

- Amazon Music decryption (`ffmpeg -decryption_key`)
- Tidal V2 manifest downloads (multi-segment)
- Parallel platform resolution (saves 5–10 s per track)
- M4A metadata tagging via `mutagen.mp4`
- Tidal endpoint rotation (`TIDAL_ALT_BASES`)
- In-process LRU metadata cache (256 entries, 10 min TTL)
- Download history (`downloads_history.json`)
- Authenticated Qobuz fallback (`QOBUZ_EMAIL` / `QOBUZ_PASSWORD`)
