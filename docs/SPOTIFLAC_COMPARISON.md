# SpotiFLAC vs RubeTunes — Comprehensive Comparison

> **Generated from source**: `xshayank/RubeTunes` (Python Rubika bot) vs `spotbye/SpotiFLAC` (Go + Wails desktop app).
>
> **Architecture note** — The two projects serve different deployment targets.
> SpotiFLAC is a **cross-platform desktop GUI** (Go + Wails, single binary, no external process).
> RubeTunes is a **Rubika chatbot** running in Docker, with a Prometheus metrics endpoint, async download queue, and multi-user rate limiting. This context is crucial when interpreting "missing" items — many RubeTunes additions (metrics, circuit breakers, rate limits) have no analogue in a single-user desktop app.

---

## Executive Summary — Biggest Gaps

| # | Gap | Direction | One-line description |
|---|-----|-----------|----------------------|
| G1 | Spotify TOTP secret scraping | RubeTunes ➕ extra | RubeTunes scrapes the TOTP secret live from Spotify's JS bundle (24 h TTL); SpotiFLAC uses only the hardcoded constant. |
| G2 | spclient GID metadata path | SpotiFLAC ➕ extra | SpotiFLAC fetches ISRC + UPC via `spclient.wg.spotify.com/metadata/4/{type}/{gid}` (base-62 → GID); RubeTunes resolves ISRC only through song.link / Soundplate / LRCLIB fallbacks. |
| G3 | Provider breadth | RubeTunes ➕ extra | RubeTunes supports **Deezer, SoundCloud, Bandcamp, Apple Music, YouTube** in addition to the three shared providers (Tidal, Qobuz, Amazon). SpotiFLAC has only Tidal + Qobuz + Amazon. |
| G4 | Lyrics download (LRC) | SpotiFLAC ➕ extra | SpotiFLAC has a full `DownloadLyrics` flow: LRCLIB (exact, no-album, search, simplified), LRC generation, file deduplication. RubeTunes fetches lyrics for tag embedding but does not write `.lrc` files. |
| G5 | Audio analysis | SpotiFLAC ➕ extra | SpotiFLAC's `analysis.go` decodes audio via ffmpeg to PCM + returns to the GUI for waveform display. No equivalent in RubeTunes. |
| G6 | In-app ffmpeg auto-install | SpotiFLAC ➕ extra | SpotiFLAC downloads and extracts ffmpeg binaries at runtime from a GitHub release. RubeTunes assumes ffmpeg is installed in the container (Dockerfile `apt-get install ffmpeg`). |
| G7 | Persistent bbolt DB for history/priority | SpotiFLAC ➕ extra | SpotiFLAC uses two embedded bbolt DBs (`history.db`, `provider_priority.db`) with up to 10 000 entries each. RubeTunes uses flat JSON files. |
| G8 | Circuit breaker + per-user rate limit | RubeTunes ➕ extra | RubeTunes has a full closed/open/half-open circuit breaker per provider and a 100-track/hour per-user rolling-window limiter. SpotiFLAC has provider sorting by last success but no circuit breaker. |
| G9 | Prometheus metrics + Sentry | RubeTunes ➕ extra | RubeTunes exposes `rubetunes_downloads_total`, `rubetunes_provider_failures_total`, histograms, and optional Sentry error tracking. SpotiFLAC has no observability beyond `fmt.Println`. |
| G10 | Disk-space guard | RubeTunes ➕ extra | RubeTunes refuses batch downloads when estimated free space is insufficient (~30 MB/track heuristic). SpotiFLAC has no guard. |
| G11 | Client-credentials (CC) token fallback | RubeTunes ➕ extra | RubeTunes falls back to `accounts.spotify.com/api/token` (CC grant) when the anon flow fails and `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` are set. SpotiFLAC only uses the anon TOTP path. |
| G12 | Standalone cover / header / avatar downloads | SpotiFLAC ➕ extra | SpotiFLAC exposes dedicated download operations for cover art, artist headers, gallery images, and avatars. RubeTunes downloads cover only inline during tagging. |
| G13 | Audio format conversion | SpotiFLAC ➕ extra | SpotiFLAC converts FLAC → MP3 / M4A (AAC or ALAC) with parallel goroutines, metadata preservation, and lyrics re-embedding. RubeTunes does not transcode. |
| G14 | macOS file-icon embedding | SpotiFLAC ➕ extra | SpotiFLAC sets the macOS Finder file icon from the cover art image. No equivalent in RubeTunes. |
| G15 | Tests | RubeTunes ➕ extra | RubeTunes ships a pytest suite, mypy type-checking, black + ruff linting via pre-commit. SpotiFLAC has no visible test infrastructure. |

---

## Section 1 — Spotify HTTP API Usage

### 1.1 Endpoint Table

| Endpoint | Method | Purpose | RubeTunes | SpotiFLAC |
|----------|--------|---------|-----------|-----------|
| `https://open.spotify.com` | GET | Scrape `clientVersion` from `<script id="appServerConfig">` base-64 JSON | ✅ `session.py:L39` | ✅ `spotfetch.go:L96` |
| `https://open.spotify.com/api/server-time` | GET | Clock-sync for TOTP (returns `serverTime` or `server_time`) | ✅ `session.py:L62` | ❌ missing |
| `https://open.spotify.com/api/token` | GET | Anonymous TOTP access token (`reason=init&productType=web-player&totp=…&totpVer=…&totpServer=…`) | ✅ `session.py:L76` | ✅ `spotfetch.go:L46` |
| `https://clienttoken.spotify.com/v1/clienttoken` | POST | Client-token header for pathfinder/v2 (`client_data.js_sdk_data` body) | ✅ `session.py:L135` | ✅ `spotfetch.go:L135` |
| `https://api-partner.spotify.com/pathfinder/v2/query` | POST | Session GraphQL (Bearer + Client-Token) — track / album / playlist / artist / search | ✅ `client.py:L134` | ✅ `spotfetch.go:L191` |
| `https://api-partner.spotify.com/pathfinder/v1/query` | GET | Persisted-query GET with SHA-256 hash (anonymous) | ✅ `spotify_meta.py:~L350` | ⚠ unknown — needs follow-up |
| `https://spclient.wg.spotify.com/metadata/4/track/{gid}` | GET | Internal GID metadata → ISRC extraction | ❌ missing | ✅ `isrc_finder.go:L117` |
| `https://spclient.wg.spotify.com/metadata/4/album/{gid}` | GET | Internal GID metadata → UPC extraction | ❌ missing | ✅ `isrc_finder.go:L129` |
| `https://accounts.spotify.com/api/token` | POST | Client-credentials grant fallback (requires `client_id`/`secret`) | ✅ `session.py:L112` | ❌ missing |

### 1.2 Common Request Headers

| Header | Value | Both? |
|--------|-------|-------|
| `User-Agent` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124–145.0.0.0 Safari/537.36` | ✅ both (version string differs slightly) |
| `Authorization` | `Bearer <access_token>` | ✅ pathfinder/v2 + spclient |
| `Client-Token` | `<client_token>` | ✅ pathfinder/v2 |
| `Spotify-App-Version` | `<clientVersion>` | ✅ both |
| `Content-Type` | `application/json;charset=UTF-8` | ✅ token endpoint |
| `Accept` | `application/json` | ✅ spclient (SpotiFLAC) |

---

## Section 2 — Authentication & Session Bootstrap

### 2.1 TOTP Details

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Secret constant | `GM3TMMJTGYZTQNZVGM4…TCMBY` (env override `SPOTIFY_TOTP_SECRET`) | Same constant hardcoded | ✅ same |
| TOTP version | 61 (`SPOTIFY_TOTP_VERSION`) | 61 (`spotifyTOTPVersion`) | ✅ same |
| Algorithm | Pure-Python HMAC-SHA1, 30 s window, 6 digits | `github.com/pquerna/otp` TOTP (same algo) | ✅ same |
| Secret scraping from JS bundle | ✅ `_try_scrape_totp_secret` — scrapes up to 5 JS bundle URLs, 24 h TTL cache | ❌ missing | ➕ RubeTunes extra |
| Server-time sync before TOTP | ✅ `session.py:L62` — `open.spotify.com/api/server-time` | ❌ uses `time.Now()` directly | ➕ RubeTunes extra |

Citations:
- `xshayank/RubeTunes: rubetunes/spotify/totp.py:L48` (TOTP algorithm)
- `xshayank/RubeTunes: rubetunes/spotify_meta.py:L154` (secret resolution)
- `spotbye/SpotiFLAC: backend/spotify_totp.go:L1` (SpotiFLAC TOTP)
- `spotbye/SpotiFLAC: backend/isrc_finder.go:L97` (isrc_finder token fetch)

### 2.2 Token Flows

| Step | RubeTunes | SpotiFLAC | Status |
|------|-----------|-----------|--------|
| 1. Scrape `clientVersion` | `session.get_session_client_version` (base-64 decode of `appServerConfig`) | `getSessionInfo` (same) | ✅ same |
| 2. Anonymous TOTP token | `session.get_anon_token` → `open.spotify.com/api/token` | `getAccessToken` (same params) | ✅ same |
| 3. CC token fallback | `session.get_cc_token` → `accounts.spotify.com/api/token` (requires env vars) | ❌ missing | ➕ RubeTunes extra |
| 4. Client token | `session.get_client_token` → `clienttoken.spotify.com/v1/clienttoken` | `getClientToken` (same payload) | ✅ same |
| Token disk cache | ❌ in-memory only | ✅ `isrc_finder.go` saves token to `~/.spotiflac/.isrc-finder-token.json` with expiry | ➕ SpotiFLAC extra |
| Token lazy init / mutex | ✅ `threading.Lock` in `SpotifyClient._lock` | ✅ `sync.Mutex` (`spotifyAnonymousTokenMu`) | ✅ same pattern |

Citations:
- `xshayank/RubeTunes: rubetunes/spotify/session.py:L76` (anon token)
- `xshayank/RubeTunes: rubetunes/spotify/session.py:L112` (CC token)
- `xshayank/RubeTunes: rubetunes/spotify/client.py:L115` (full auth chain)
- `spotbye/SpotiFLAC: backend/spotfetch.go:L174` (Initialize)
- `spotbye/SpotiFLAC: backend/isrc_finder.go:L95` (disk token cache)

---

## Section 3 — Metadata Fetching

### 3.1 GraphQL Operations

| Operation | Hash | RubeTunes | SpotiFLAC | Status |
|-----------|------|-----------|-----------|--------|
| `getTrack` | SHA-256 (persisted) | ✅ `_fetch_track_graphql` | ✅ `spotify_metadata.go` | ✅ same |
| `getAlbum` | SHA-256 | ✅ `_fetch_album_graphql_page` (paginated, offset+limit) | ✅ paginated | ✅ same |
| `fetchPlaylist` | SHA-256 | ✅ `_fetch_playlist_graphql_page` | ✅ | ✅ same |
| `queryArtistOverview` | SHA-256 | ✅ `_fetch_artist_overview_graphql` | ✅ | ✅ same |
| `queryArtistDiscographyAll` | SHA-256 | ✅ `_fetch_artist_discography_graphql` | ✅ | ✅ same |
| `searchDesktop` | SHA-256 | ✅ `_fetch_search_graphql` | ✅ | ✅ same |

### 3.2 ISRC / UPC Resolution

| Path | RubeTunes | SpotiFLAC | Status |
|------|-----------|-----------|--------|
| `spclient.wg.spotify.com/metadata/4/track/{gid}` → `external_id[type==isrc]` | ❌ missing | ✅ `isrc_finder.go:L215` | ❌ RubeTunes missing |
| `spclient.wg.spotify.com/metadata/4/album/{gid}` → `external_id[type==upc]` | ❌ missing | ✅ `isrc_finder.go:L252` | ❌ RubeTunes missing |
| song.link / Odesli `linksByPlatform` ISRC field | ✅ `resolver.py` | ✅ `songlink.go` | ✅ same |
| Soundplate fallback ISRC | ✅ `spotify_meta.py` (`_isrc_soundplate`) | ✅ `songlink.go` (`lookupSpotifyISRCViaSoundplate`) | ✅ same |
| Deezer ISRC API (`/track/isrc:{ISRC}`) | ✅ `resolver.py` | ✅ `songlink.go:L145` | ✅ same |
| ISRC disk cache | ✅ `cache.py` (`_get_cached_isrc` / `_put_cached_isrc`, JSON file) | ✅ `isrc_finder.go` (JSON file in `~/.spotiflac/`) | ✅ same pattern |
| UPC lookup | ❌ missing | ✅ via album GID metadata | ❌ RubeTunes missing |

### 3.3 Metadata Field Mapping

| Field | RubeTunes tag key | SpotiFLAC `Metadata` field | Status |
|-------|-------------------|---------------------------|--------|
| Title | `title` | `Title` | ✅ same |
| Artist(s) | `artists` (list) | `Artist` (separator-joined string) | ⚠ different type |
| Album | `album` | `Album` | ✅ same |
| Album artist | `albumartist` / `album_artist` | `AlbumArtist` | ✅ same |
| Release date | `release_date` | `Date` | ✅ same |
| Track number | `track_number` | `TrackNumber` | ✅ same |
| Total tracks | `track_total` | `TotalTracks` | ✅ same |
| Disc number | `disc_number` | `DiscNumber` | ✅ same |
| Total discs | — | `TotalDiscs` | ❌ RubeTunes missing |
| ISRC | `isrc` | `ISRC` | ✅ same |
| UPC | `upc` | `UPC` | ⚠ RubeTunes has field, no resolution path |
| Genre | `genre` (from MusicBrainz via resolver) | `Genre` (from MusicBrainz in provider flow) | ✅ same source |
| Copyright | — | `Copyright` | ❌ RubeTunes missing |
| Publisher / Label | — | `Publisher` | ❌ RubeTunes missing |
| Composer | — | `Composer` | ❌ RubeTunes missing |
| Spotify URL | `comment` | `URL` + `Comment` | ⚠ partial |
| Description | — | `"https://github.com/spotbye/SpotiFLAC"` | ❌ RubeTunes missing |
| Lyrics (embedded) | `lyrics` → USLT/vorbis | `Lyrics` in FLAC vorbis | ✅ both embed |
| Multi-artist separator | hardcoded `", "` | `Separator` (configurable) | ⚠ SpotiFLAC configurable |

Citations:
- `xshayank/RubeTunes: rubetunes/tagging.py:L56` (ID3 field mapping)
- `xshayank/RubeTunes: rubetunes/tagging.py:L79` (FLAC field mapping)
- `spotbye/SpotiFLAC: backend/metadata.go:L1` (Metadata struct)
- `spotbye/SpotiFLAC: backend/qobuz.go:L178` (metadata construction in Qobuz download)

---

## Section 4 — Download / Streaming Resolution

### 4.1 Provider Overview

| Provider | RubeTunes | SpotiFLAC | Status |
|----------|-----------|-----------|--------|
| **Tidal** | ✅ `providers/tidal.py` + `providers/tidal_alt.py` | ✅ `tidal.go` + `tidal_alt.go` | ✅ both |
| **Qobuz** | ✅ `providers/qobuz.py` | ✅ `qobuz.go` + `qobuz_api.go` | ✅ both |
| **Amazon Music** | ✅ `providers/amazon.py` | ✅ `amazon.go` | ✅ both |
| **Deezer** | ✅ `providers/deezer.py` (requires `DEEZER_ARL`) | ❌ missing (only used for ISRC lookup) | ❌ SpotiFLAC missing |
| **SoundCloud** | ✅ `providers/soundcloud.py` | ❌ missing | ❌ SpotiFLAC missing |
| **Bandcamp** | ✅ `providers/bandcamp.py` | ❌ missing | ❌ SpotiFLAC missing |
| **Apple Music** | ✅ `providers/apple_music.py` | ❌ missing | ❌ SpotiFLAC missing |
| **YouTube (yt-dlp)** | ✅ `providers/youtube.py` (via yt-dlp) | ❌ missing | ❌ SpotiFLAC missing |

### 4.2 Qobuz Quality Chain

| Quality Code | Meaning | RubeTunes | SpotiFLAC | Status |
|-------------|---------|-----------|-----------|--------|
| `27` | Hi-Res 24-bit (MQA / up to 192 kHz) | ✅ `flac_hi` quality | ✅ quality `"27"` | ✅ same |
| `7` | 24-bit Standard FLAC | ✅ fallback | ✅ fallback from 27 | ✅ same |
| `6` | 16-bit CD Lossless FLAC | ✅ `flac_cd` quality | ✅ fallback from 7 | ✅ same |
| `5` | MP3 320 k | ✅ `mp3` quality | ⚠ defaults to `6` | ⚠ partial |
| Quality fallback chain | 27 → 7 → 6 (configurable) | 27 → 7 → 6 (`allowFallback`) | ✅ same |
| Signed request (MD5 / ts / token) | ✅ `qobuz.py` | ✅ `qobuz_api.go` (`doQobuzSignedRequest`) | ✅ same |
| Multiple API base URLs (prioritised) | ✅ circuit breaker + priority | ✅ `prioritizeProviders("qobuz", …)` | ✅ same pattern |

Citations:
- `xshayank/RubeTunes: rubetunes/providers/qobuz.py:L1`
- `spotbye/SpotiFLAC: backend/qobuz.go:L130` (quality chain)

### 4.3 Tidal

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Public API (`api.tidal.com/v1`) | ✅ `providers/tidal.py` (requires `TIDAL_TOKEN`) | ✅ `tidal.go` | ✅ same |
| Alt/unofficial APIs (hifi-api, dabmusic) | ✅ `providers/tidal_alt.py` | ✅ `tidal_alt.go` + `tidal_api_list.go` | ✅ same pattern |
| Quality: FLAC / Hi-Res / Dolby Atmos | ✅ | ✅ | ✅ same |
| Token requirement | `TIDAL_TOKEN` env var | External API (no user token needed) | ⚠ different |

### 4.4 Amazon Music

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| ASIN resolution | song.link → Amazon URL → ASIN regex | song.link → Amazon URL → ASIN regex | ✅ same |
| Stream URL source | dabmusic.xyz / afkarxyz API | `dabmusic.xyz` (`amazon.go`) | ✅ same |
| DRM decryption key | ✅ ffmpeg `-decryption_key` | ✅ ffmpeg `-decryption_key` | ✅ same |
| Codec detection (FLAC vs AAC) | ✅ ffprobe | ✅ ffprobe | ✅ same |
| MusicBrainz genre fetch | ✅ `resolver.py` | ✅ `amazon.go:L55` goroutine | ✅ same |

### 4.5 Tagging / Format Support

| Format | RubeTunes library | SpotiFLAC library | Status |
|--------|------------------|-------------------|--------|
| FLAC | mutagen FLAC + vorbis comments | `go-flac` + `flacvorbis` + `flacpicture` | ✅ same fields |
| MP3 | mutagen ID3 | `bogem/id3v2` | ✅ same fields |
| M4A | mutagen MP4 (ffmpeg fallback) | ffmpeg remux | ⚠ same outcome, different path |
| Cover art (embedded) | ✅ JPEG embed | ✅ JPEG embed, optional max-resolution upscale | ⚠ SpotiFLAC has max-res upgrade |
| Lyrics (embedded) | ✅ USLT (MP3) / vorbis (FLAC) | ✅ vorbis `LYRICS` tag | ✅ same |
| Spotify URL resolution prefix upgrade | ✅ `ab67616d00001e02` → `ab67616d0000b273` | ✅ same three size constants | ✅ same |

### 4.6 Filename Format Tokens

| Token | RubeTunes | SpotiFLAC | Status |
|-------|-----------|-----------|--------|
| `{title}` | ✅ | ✅ | ✅ same |
| `{artist}` | ✅ | ✅ | ✅ same |
| `{album}` | ✅ | ✅ | ✅ same |
| `{album_artist}` | ✅ | ✅ | ✅ same |
| `{year}` | ✅ | ✅ | ✅ same |
| `{date}` | ✅ | ✅ | ✅ same |
| `{track}` (zero-padded) | ✅ | ✅ | ✅ same |
| `{disc}` | ✅ | ✅ | ✅ same |
| `{isrc}` | ✅ | ✅ | ✅ same |
| Predefined presets (`title-artist`, `artist-title`, `title`) | ✅ | ✅ | ✅ same |

---

## Section 5 — Concurrency, Rate Limiting, Retries

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Runtime concurrency model | Python asyncio + `ThreadPoolExecutor` (8 workers) | Go goroutines (`sync.WaitGroup`, `sync.Mutex`) | ⚠ different model, equivalent throughput |
| Provider waterfall (auto-fallback) | ✅ `downloader.build_platform_choices` → sorted list | ✅ sorted by `prioritizeProviders` then per-provider fallback | ✅ same concept |
| Circuit breaker (open/half-open/closed) | ✅ `circuit_breaker.py` — 3 failures / 5-min window → 10-min open | ❌ missing | ➕ RubeTunes extra |
| Per-user rate limit | ✅ `rate_limiter.py` — 100 tracks/hour rolling window | ❌ missing (single-user app) | N/A for desktop |
| Provider priority (success-weighted) | ✅ disk JSON `provider_stats.json`, `_prioritize_providers` | ✅ bbolt `provider_priority.db`, `prioritizeProviders` | ✅ same concept |
| MusicBrainz pre-flight guard | ✅ `resolver.py` — 60 s availability cache, rate-limit (1 req/s) | ✅ `ShouldSkipMusicBrainzMetadataFetch` | ✅ same |
| HTTP client timeouts | ✅ per-request `timeout=` in requests | ✅ `http.Client{Timeout: N}` | ✅ both |
| Retry on 5xx | ❌ not explicit | ❌ not explicit | ❌ both missing |
| Disk-space guard | ✅ `disk_guard.py` (30 MB/track heuristic, 2× headroom) | ❌ missing | ➕ RubeTunes extra |
| Async download queue | ✅ `downloader.py` asyncio queue | ❌ sequential per-track GUI call | ➕ RubeTunes extra |

---

## Section 6 — Caching

| Cache | RubeTunes | SpotiFLAC | Status |
|-------|-----------|-----------|--------|
| Track-info in-memory LRU | ✅ `cache.py` — 256 entries, 10-min TTL, `OrderedDict` | ❌ missing (`recent_fetches.go` is a GUI UI cache, not a query cache) | ➕ RubeTunes extra |
| ISRC disk cache | ✅ `cache.py` — JSON file `spotify-isrc-cache.json` | ✅ `isrc_finder.go` — JSON file `.isrc-finder-token.json` area | ✅ same |
| Spotify anonymous token disk cache | ❌ in-memory only | ✅ `isrc_finder.go:L87` — JSON with `AccessTokenExpirationTimestampMs` | ➕ SpotiFLAC extra |
| Download history | ✅ `history.py` — JSON file, key = `track_id|source|quality` | ✅ `history.go` — bbolt DB, `DownloadHistory` bucket, max 10 000 entries | ⚠ same concept, bbolt is more robust |
| Fetch history (URL) | ❌ missing | ✅ `history.go` — bbolt `FetchHistory` bucket, dedup by URL+type | ➕ SpotiFLAC extra |
| Recent UI fetches | ❌ missing | ✅ `recent_fetches.go` — JSON file, per-session | ➕ SpotiFLAC extra (GUI-specific) |
| Provider priority stats | ✅ `circuit_breaker.py` — JSON `provider_stats.json` | ✅ `provider_priority.go` — bbolt `ProviderPriority` bucket | ✅ same concept |
| File-exists fast-path | ✅ `history.py._check_download_history` | ✅ `ResolveOutputPathForDownload` / `EXISTS:` sentinel | ✅ same |

---

## Section 7 — Error Model & Logging

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Structured logging | ✅ `logging_setup.py` — `LOG_FORMAT=json` (via `python-json-logger`) or plain text | ❌ `fmt.Println` / `fmt.Printf` only | ➕ RubeTunes extra |
| Log levels | DEBUG / INFO / WARNING / ERROR via Python `logging` | No log levels — all stdout | ❌ SpotiFLAC missing levels |
| Sentinel errors | ✅ `DownloadError(source, message)` | ✅ `var SpotifyError = errors.New("spotify error")` | ✅ same concept |
| Error wrapping | Python `raise RuntimeError(…)` / `except … as exc` | Go `fmt.Errorf("%w: …", err)` | ✅ both use wrapping |
| Sentry error tracking | ✅ `sentry_setup.py` — SENTRY_DSN env var, 0.1 traces sample rate | ❌ missing | ➕ RubeTunes extra |
| Prometheus metrics | ✅ `metrics.py` — counters, histograms, gauges at `:9090/metrics` | ❌ missing | ➕ RubeTunes extra |
| Progress output | ✅ bot messages (Rubika) | ✅ `fmt.Printf` stdout (GUI consumes) | ✅ both signal progress |
| Circuit-breaker state API | ✅ `circuit_breaker.get_breaker_states()` | ❌ missing | ➕ RubeTunes extra |

---

## Section 8 — Configuration & Secrets

### 8.1 SpotiFLAC Configuration

SpotiFLAC stores configuration in `~/.spotiflac/config.json` (read via `config.go`).

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `linkResolver` | string | `"songlink"` | Which link resolver to use (`songlink` or `songstats`) |
| `allowResolverFallback` | bool | `true` | Whether to try alternate resolvers on failure |
| `redownloadWithSuffix` | bool | `false` | Append suffix instead of skipping existing files |
| Output directory | string | `~/Music` | Default download path |
| Filename format | string | `"title-artist"` | Template string |
| Metadata separator | string | `", "` | Artist separator in tags |

No secrets required — SpotiFLAC accesses all streaming services anonymously via third-party proxy APIs.

### 8.2 RubeTunes Configuration (Environment Variables)

| Variable | Required | Description |
|----------|----------|-------------|
| `RUBIKA_BOT_TOKEN` | ✅ yes | Rubika / rubpy bot token |
| `SPOTIFY_CLIENT_ID` | ⚠ optional | CC token fallback only |
| `SPOTIFY_CLIENT_SECRET` | ⚠ optional | CC token fallback only |
| `SPOTIFY_TOTP_SECRET` | ⚠ optional | Override hardcoded TOTP secret |
| `DEEZER_ARL` | ⚠ optional | Enables Deezer provider |
| `QOBUZ_EMAIL` | ⚠ optional | Enables Qobuz authenticated access |
| `QOBUZ_PASSWORD` | ⚠ optional | Enables Qobuz authenticated access |
| `TIDAL_TOKEN` | ⚠ optional | Enables Tidal API provider |
| `SENTRY_DSN` | ⚠ optional | Enables Sentry error reporting |
| `LOG_FORMAT` | ⚠ optional | `text` (default) or `json` |
| `METRICS_PORT` | ⚠ optional | Prometheus port (default `9090`, `0` to disable) |
| `USER_TRACKS_PER_HOUR` | ⚠ optional | Rate limit per user (default `100`) |
| `HEURISTIC_MB_PER_TRACK` | ⚠ optional | Disk guard heuristic (default `30` MB) |
| `MIN_FREE_SPACE_MB` | ⚠ optional | Absolute minimum free space (default `500` MB) |
| `CIRCUIT_FAIL_THRESHOLD` | ⚠ optional | Failures before circuit opens (default `3`) |
| `CIRCUIT_FAIL_WINDOW_SEC` | ⚠ optional | Failure counting window (default `300` s) |
| `CIRCUIT_OPEN_DURATION_SEC` | ⚠ optional | How long circuit stays open (default `600` s) |

---

## Section 9 — Public REST / HTTP Surface

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Primary user interface | Rubika bot commands (chat messages) | Wails desktop GUI (local webview) | ❌ fundamentally different |
| External HTTP clients | Rubika users via chat; no open REST endpoint | None — all local | N/A |
| Prometheus metrics endpoint | ✅ `http://:<METRICS_PORT>/metrics` (default `:9090`) | ❌ missing | ➕ RubeTunes extra |
| Docker EXPOSE | `9090` (metrics) | N/A — native app | N/A |
| Wails bindings | N/A | ✅ `app.go` binds all backend functions to GUI | N/A |
| Multi-user support | ✅ user GUID isolation, per-user rate limits | ❌ single-user app | ➕ RubeTunes extra |

---

## Section 10 — Packaging, Runtime, Dependencies

### 10.1 SpotiFLAC (`go.mod`)

| Dependency | Version | Purpose |
|-----------|---------|---------|
| `github.com/bogem/id3v2/v2` | v2.1.4 | ID3 tagging |
| `github.com/go-flac/flacpicture` | v0.3.0 | FLAC cover art |
| `github.com/go-flac/flacvorbis` | v0.2.0 | FLAC vorbis comments |
| `github.com/go-flac/go-flac` | v1.0.0 | FLAC container |
| `github.com/pquerna/otp` | v1.5.0 | TOTP / OTP |
| `github.com/ulikunitz/xz` | v0.5.15 | XZ decompression (ffmpeg archive) |
| `github.com/wailsapp/wails/v2` | v2.11.0 | Desktop GUI framework |
| `go.etcd.io/bbolt` | v1.4.3 | Embedded key-value DB (history, priority) |
| `golang.org/x/image` | v0.12.0 | Image resize (macOS icon) |
| `golang.org/x/text` | v0.31.0 | Unicode normalization |
| Go version | 1.26 | |
| ffmpeg | self-downloaded at runtime | Audio decode / decrypt / convert |

### 10.2 RubeTunes (`requirements.txt`)

| Dependency | Version | Purpose |
|-----------|---------|---------|
| `pyrogram` | ==2.0.106 | Telegram-compatible client (not used directly) |
| `tgcrypto` | ==1.2.5 | Crypto for Pyrogram |
| `rubpy` | ==7.3.4 | Rubika bot framework |
| `python-dotenv` | ==1.2.2 | `.env` loading |
| `requests` | >=2.30.0 | HTTP client |
| `mutagen` | ==1.47.0 | Audio tagging (FLAC, MP3, M4A) |
| `python-json-logger` | >=2.0 | Structured JSON logging |
| `prometheus-client` | >=0.20 | Metrics endpoint |
| `sentry-sdk` | >=2.0 | Error tracking |
| Python version | 3.11 (Docker) | |
| ffmpeg | `apt-get install ffmpeg` (Dockerfile) | Audio decode / decrypt |
| yt-dlp | installed in Dockerfile | YouTube provider |

### 10.3 Deployment

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Distribution | Docker image | Single native binary (Windows/macOS/Linux) | ❌ different |
| Runtime isolation | Container | Native process | N/A |
| ffmpeg | Pre-installed in Docker image | Downloaded at runtime on demand | ⚠ different |
| Volumes | `/app/downloads`, `/app/state.json`, `/app/downloads_history.json` | `~/Music` + `~/.spotiflac/` | ⚠ different |
| Multi-arch | Linux amd64/arm64 (via Docker base) | Windows/macOS/Linux (platform-specific binaries) | ⚠ different |

---

## Section 11 — Tests & Tooling

| Aspect | RubeTunes | SpotiFLAC | Status |
|--------|-----------|-----------|--------|
| Unit tests | ✅ `tests/test_spotify_pathfinder.py`, `tests/test_spotify_dl.py` | ❌ no test directory visible | ➕ RubeTunes extra |
| Test framework | pytest | — | ➕ RubeTunes extra |
| Type checking | ✅ mypy (`mypy.ini`) | ❌ Go type system (compile-time) | N/A (language difference) |
| Linter | ✅ ruff | `go vet` / `staticcheck` (not configured) | ⚠ partial |
| Formatter | ✅ black | `gofmt` (standard) | ✅ both have formatters |
| Pre-commit hooks | ✅ `.pre-commit-config.yaml` (black, ruff, mypy) | ❌ not configured | ➕ RubeTunes extra |
| TOTP known-vector pin | ✅ `totp.py:L101` — `KNOWN_VECTOR_CODE` computed at import, tested | ❌ missing | ➕ RubeTunes extra |
| CI / CD | unknown — needs follow-up | unknown — needs follow-up | unknown |

---

## Action Items — Bringing RubeTunes to Parity with SpotiFLAC

The following checklist targets features present in SpotiFLAC that are **missing or weaker** in RubeTunes.

### High Priority (correctness / completeness)

- [ ] **A1 — spclient GID metadata path** (`isrc_finder.go`)
  Implement `spclient.wg.spotify.com/metadata/4/track/{gid}` lookup using base-62 → GID conversion.
  This gives a direct ISRC without depending on song.link rate limits.
  Cite: `spotbye/SpotiFLAC: backend/isrc_finder.go:L215`

- [ ] **A2 — UPC resolution**
  Implement album GID metadata fetch (`spclient.wg.spotify.com/metadata/4/album/{gid}`) to extract UPC and embed it in FLAC/MP3/M4A tags.
  Cite: `spotbye/SpotiFLAC: backend/isrc_finder.go:L252`

- [ ] **A3 — Missing FLAC tag fields**
  Add `TotalDiscs`, `Copyright`, `Publisher`, `Composer`, and `Description` fields to `tagging.embed_metadata`.
  All are present in SpotiFLAC `Metadata` struct; absence causes lossy round-trips when switching tools.
  Cite: `spotbye/SpotiFLAC: backend/metadata.go:L1`

- [ ] **A4 — Configurable artist separator**
  SpotiFLAC's `Separator` field is user-configurable. Hardcoding `", "` in RubeTunes produces different output than SpotiFLAC default.
  Cite: `spotbye/SpotiFLAC: backend/qobuz.go:L178`

### Medium Priority (usability)

- [ ] **B1 — LRC lyrics file download**
  Port `LyricsClient.DownloadLyrics` + `ConvertToLRC` to write `.lrc` sidecar files alongside audio files.
  RubeTunes currently only embeds lyrics in tags.
  Cite: `spotbye/SpotiFLAC: backend/lyrics.go:L245`

- [ ] **B2 — Spotify token disk cache**
  Cache the anonymous access token to disk with `AccessTokenExpirationTimestampMs` to avoid a full TOTP round-trip on every bot restart.
  Cite: `spotbye/SpotiFLAC: backend/isrc_finder.go:L87`

- [ ] **B3 — Fetch history (URL dedup)**
  Add a `FetchHistory` store keyed by URL+type to avoid re-fetching metadata for recently queried URLs.
  Cite: `spotbye/SpotiFLAC: backend/history.go:L144`

- [ ] **B4 — Max-resolution cover art upgrade**
  Implement `spotifySize300 → spotifySize640 → spotifySizeMax` URL substitution when `embedMaxQualityCover` is enabled.
  Cite: `spotbye/SpotiFLAC: backend/cover.go:L115`

- [ ] **B5 — Retry on transient 5xx**
  Both projects lack automatic HTTP retry for 5xx responses. Implement exponential back-off with jitter for provider HTTP calls.

### Low Priority / Nice-to-have

- [ ] **C1 — Audio analysis endpoint**
  Expose an internal command to run ffprobe + PCM decode for debugging download quality (bit depth, sample rate, dynamic range).
  Cite: `spotbye/SpotiFLAC: backend/analysis.go:L1`

- [ ] **C2 — Standalone cover / avatar download command**
  Add bot commands to download cover art, artist headers, and avatar JPEGs without downloading the full track.
  Cite: `spotbye/SpotiFLAC: backend/cover.go:L152`

- [ ] **C3 — Audio format conversion**
  Add a bot command to transcode downloaded FLAC files to MP3 / M4A with metadata preservation.
  Cite: `spotbye/SpotiFLAC: backend/ffmpeg.go:L241` (`ConvertAudio`)

- [ ] **C4 — bbolt history DB**
  Migrate `history.py` from flat JSON to bbolt-equivalent (e.g. SQLite via `sqlite3` stdlib) for better concurrency safety and support for 10 000+ entries.
  Cite: `spotbye/SpotiFLAC: backend/history.go:L1`

---

## Feature Parity Table — Full Overview

| Feature | RubeTunes | SpotiFLAC | Status |
|---------|-----------|-----------|--------|
| TOTP auth (anon token) | ✅ | ✅ | ✅ same |
| TOTP secret scraping from JS | ✅ | ❌ | ➕ RT extra |
| Server-time clock sync | ✅ | ❌ | ➕ RT extra |
| CC token fallback | ✅ | ❌ | ➕ RT extra |
| Token disk cache | ❌ | ✅ | ❌ RT missing |
| pathfinder/v2 GraphQL | ✅ | ✅ | ✅ same |
| pathfinder/v1 persisted queries | ✅ | ⚠ | ⚠ partial |
| spclient GID ISRC/UPC | ❌ | ✅ | ❌ RT missing |
| song.link cross-platform resolve | ✅ | ✅ | ✅ same |
| Soundplate ISRC fallback | ✅ | ✅ | ✅ same |
| ISRC disk cache | ✅ | ✅ | ✅ same |
| Tidal provider | ✅ | ✅ | ✅ same |
| Qobuz provider | ✅ | ✅ | ✅ same |
| Amazon Music provider | ✅ | ✅ | ✅ same |
| Deezer provider | ✅ | ❌ | ➕ RT extra |
| SoundCloud provider | ✅ | ❌ | ➕ RT extra |
| Bandcamp provider | ✅ | ❌ | ➕ RT extra |
| Apple Music provider | ✅ | ❌ | ➕ RT extra |
| YouTube (yt-dlp) provider | ✅ | ❌ | ➕ RT extra |
| Qobuz quality 27→7→6 chain | ✅ | ✅ | ✅ same |
| Qobuz signed requests | ✅ | ✅ | ✅ same |
| Amazon ASIN resolution | ✅ | ✅ | ✅ same |
| Amazon ffmpeg DRM decrypt | ✅ | ✅ | ✅ same |
| MusicBrainz genre fetch | ✅ | ✅ | ✅ same |
| FLAC tagging | ✅ mutagen | ✅ go-flac | ✅ same |
| MP3 ID3 tagging | ✅ mutagen | ✅ id3v2 | ✅ same |
| M4A tagging | ✅ mutagen + ffmpeg fallback | ✅ ffmpeg | ⚠ partial |
| Copyright/Publisher/Composer tags | ❌ | ✅ | ❌ RT missing |
| UPC tag | ⚠ field exists, no source | ✅ | ⚠ partial |
| TotalDiscs tag | ❌ | ✅ | ❌ RT missing |
| Cover art embed | ✅ | ✅ | ✅ same |
| Max-res cover upgrade | ❌ | ✅ | ❌ RT missing |
| Standalone cover download | ❌ | ✅ | ❌ RT missing |
| Artist header/avatar download | ❌ | ✅ | ❌ RT missing |
| Lyrics (tag embed) | ✅ | ✅ | ✅ same |
| Lyrics (LRC sidecar) | ❌ | ✅ | ❌ RT missing |
| LRC multi-source fallback (LRCLIB) | ✅ | ✅ | ✅ same |
| Filename format tokens | ✅ | ✅ | ✅ same |
| Provider priority sort | ✅ JSON stats | ✅ bbolt DB | ✅ same |
| Circuit breaker | ✅ | ❌ | ➕ RT extra |
| Per-user rate limiting | ✅ | ❌ | N/A (desktop) |
| Disk space guard | ✅ | ❌ | ➕ RT extra |
| Download history | ✅ JSON | ✅ bbolt | ⚠ same concept |
| Fetch history / dedup | ❌ | ✅ | ❌ RT missing |
| LRU track-info cache | ✅ | ❌ | ➕ RT extra |
| Prometheus metrics | ✅ | ❌ | ➕ RT extra |
| Sentry error tracking | ✅ | ❌ | ➕ RT extra |
| Structured JSON logging | ✅ | ❌ | ➕ RT extra |
| Audio analysis (ffprobe + PCM) | ❌ | ✅ | ❌ RT missing |
| Audio format conversion | ❌ | ✅ | ❌ RT missing |
| In-app ffmpeg auto-install | ❌ (Dockerfile) | ✅ | N/A (container) |
| macOS file icon embedding | ❌ | ✅ | N/A (Linux Docker) |
| Tests (pytest / unit) | ✅ | ❌ | ➕ RT extra |
| Pre-commit linting | ✅ | ❌ | ➕ RT extra |

---

*Document generated by cross-reading source files from both repositories.
All claims are traceable to the cited files and line numbers.*
