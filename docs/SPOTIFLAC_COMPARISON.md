# SpotiFLAC ↔ RubeTunes Backend Comparison

> **Purpose**: This document is the specification for porting the remaining SpotiFLAC backend
> behaviours into `xshayank/RubeTunes`. All cited references follow the permalink format
> `owner/repo/path/to/file#L<start>-L<end>`.
>
> **Snapshot dates**: SpotiFLAC `30cbcf8` · RubeTunes `HEAD` on the PR branch.

---

## Table of Contents

1. [Repo Layout & Entry Points](#1-repo-layout--entry-points)
2. [Configuration & Env Vars](#2-configuration--env-vars)
3. [Spotify Auth Chain](#3-spotify-auth-chain)
4. [TOTP Generation](#4-totp-generation)
5. [Spotify API Surface](#5-spotify-api-surface)
6. [Metadata Operations](#6-metadata-operations)
7. [ID / URI Handling](#7-id--uri-handling)
8. [Download / Streaming Pipeline](#8-download--streaming-pipeline)
9. [Concurrency, Queueing, Rate-Limiting & Retries](#9-concurrency-queueing-rate-limiting--retries)
10. [Caching](#10-caching)
11. [Error Handling & Logging](#11-error-handling--logging)
12. [HTTP Routes / Handlers Exposed](#12-http-routes--handlers-exposed)
13. [Models / Schemas](#13-models--schemas)
14. [Dependencies](#14-dependencies)
15. [Docker / Runtime](#15-docker--runtime)
16. [Tests](#16-tests)
17. [Security / Secrets Handling](#17-security--secrets-handling)
18. [Miscellaneous](#18-miscellaneous)
19. [Port Checklist](#port-checklist)

---

## 1. Repo Layout & Entry Points

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Language / runtime** | Go 1.26 | Python 3.11 |
| **Delivery model** | Wails v2 desktop GUI (frameless, 1024×600) | Rubika messenger bot |
| **Primary entry point** | `spotbye/SpotiFLAC/main.go` — `wails.Run(...)` | `xshayank/RubeTunes/main.py` — subprocess-spawns `rub.py` |
| **Main application logic** | `spotbye/SpotiFLAC/app.go` (59 KB, `App` struct with Wails RPC bindings) | `xshayank/RubeTunes/rub.py` (101 KB, Rubika event loop, command router, queue) |
| **Backend package** | `spotbye/SpotiFLAC/backend/` — ~35 `.go` source files | `xshayank/RubeTunes/rubetunes/` — 15+ Python modules + `providers/` sub-package |
| **Shim / compat layer** | none | `xshayank/RubeTunes/spotify_dl.py` — star-imports all `rubetunes` submodules so existing `import spotify_dl` callers keep working |
| **Frontend** | `spotbye/SpotiFLAC/frontend/` (Vue/React, built with pnpm) | n/a (Rubika client is the UI) |
| **Module manifest** | `spotbye/SpotiFLAC/go.mod` — `module github.com/afkarxyz/SpotiFLAC` | `xshayank/RubeTunes/pyproject.toml` + `requirements.txt` |
| **Version** | `7.1.5` (from `spotbye/SpotiFLAC/wails.json`) | `2.0.0` (from `xshayank/RubeTunes/rubetunes/__init__.py`) |
| **IPC / server** | Wails runtime events (`runtime.EventsEmit`) — no network server | Rubika long-poll; Prometheus metrics HTTP on port 9090 |
| **Persistent storage root** | Platform app-data directory (Go `os.UserHomeDir` + subdir) | `/tmp/tele2rub/` for caches; `/app/downloads/` for files; `state.json` CWD |
| **Graceful shutdown** | OS-handled (single binary) | `main.py` sends SIGTERM → child; waits `SHUTDOWN_TIMEOUT_SEC` (30 s); SIGKILL if needed; saves `queue_snapshot.json` |

### Gap / Action

- SpotiFLAC is a **desktop GUI**; RubeTunes is a **headless bot**. There is no direct UI to port.
  Progress events emitted by SpotiFLAC via `runtime.EventsEmit` map to Rubika message edits in RubeTunes.
- The `backend/` Go package is the functional unit to study; `app.go` is the orchestration glue whose equivalent in RubeTunes is the combination of `rub.py` + `rubetunes/resolver.py` + `rubetunes/downloader.py`.

---

## 2. Configuration & Env Vars

### SpotiFLAC — `spotbye/SpotiFLAC/backend/config.go`

JSON file `config.json` stored in the platform app-data directory.  All values are read at
runtime and mutations are persisted immediately.

| Key | Default | Purpose |
|---|---|---|
| `redownloadWithSuffix` | `false` | Append ` (N)` to filename instead of overwriting existing file |
| `linkResolver` | `"songlink"` | Choose between `"songlink"` (Deezer→Odesli) or `"songstats"` |
| `allowResolverFallback` | `true` | Try second resolver if primary fails |

No environment variables are read by SpotiFLAC. All configuration is via the JSON file
exposed through Wails RPC (`GetLinkResolverSetting`, `SetLinkResolverSetting`,
`GetLinkResolverAllowFallback`, `SetLinkResolverAllowFallback`,
`GetRedownloadWithSuffixSetting`).

### RubeTunes — `xshayank/RubeTunes/.env.example` + modules

All configuration is via environment variables (loaded by `python-dotenv`).

| Variable | Default | Module | Purpose |
|---|---|---|---|
| `RUBIKA_SESSION` | `rubika_session` | `rub.py` | Rubika session file name |
| `RUBIKA_PHONE` | — | `rub.py` | Phone number for Rubika auth |
| `ADMIN_GUIDS` | — | `rub.py` | Comma-separated admin GUIDs |
| `SPOTIFY_CLIENT_ID` | `""` | `rubetunes/spotify_meta.py:116` | OAuth2 client ID (CC fallback) |
| `SPOTIFY_CLIENT_SECRET` | `""` | `rubetunes/spotify_meta.py:117` | OAuth2 client secret (CC fallback) |
| `SPOTIFY_TOTP_SECRET` | hardcoded | `rubetunes/spotify_meta.py:156` | Override the TOTP secret without code change |
| `DEEZER_ARL` | `""` | `rubetunes/spotify_meta.py:118` | Deezer auth cookie (enables FLAC downloads) |
| `QOBUZ_EMAIL` | `""` | `rubetunes/spotify_meta.py:119` | Qobuz login (authenticated fallback) |
| `QOBUZ_PASSWORD` | `""` | `rubetunes/spotify_meta.py:120` | Qobuz password |
| `TIDAL_TOKEN` | — | `rubetunes/providers/tidal.py` | Tidal API bearer token |
| `TIDAL_ALT_BASES` | 4 hardcoded mirrors | `rubetunes/providers/tidal_alt.py` | Comma-separated Tidal proxy base URLs |
| `METRICS_PORT` | `9090` | `rubetunes/metrics.py` | Prometheus HTTP port (0 = disable) |
| `LOG_FORMAT` | `text` | `rubetunes/logging_setup.py` | `text` or `json` |
| `SENTRY_DSN` | — | `rubetunes/sentry_setup.py` | Sentry DSN for error reporting |
| `SHUTDOWN_TIMEOUT_SEC` | `30` | `main.py` | Seconds to wait before SIGKILL |
| `BATCH_CONCURRENCY` | `3` | `rub.py` | Parallel batch downloads (1–6) |
| `USER_TRACKS_PER_HOUR` | `100` | `rubetunes/rate_limiter.py` | Per-user rolling rate limit |
| `HEURISTIC_MB_PER_TRACK` | `30` | `rubetunes/disk_guard.py` | Disk estimate per track (MB) |
| `MIN_FREE_SPACE_MB` | `500` | `rubetunes/disk_guard.py` | Minimum free disk space (MB) |
| `MIN_FREE_SPACE_MULTIPLIER` | `2.0` | `rubetunes/disk_guard.py` | Free-space safety multiplier |
| `CIRCUIT_FAIL_THRESHOLD` | `3` | `rubetunes/circuit_breaker.py:43` | Failures before opening circuit |
| `CIRCUIT_FAIL_WINDOW_SEC` | `300` | `rubetunes/circuit_breaker.py:44` | Failure counting window (s) |
| `CIRCUIT_OPEN_DURATION_SEC` | `600` | `rubetunes/circuit_breaker.py:45` | Duration circuit stays open (s) |
| `ZIP_PART_SIZE_BYTES` / `ZIP_PART_SIZE_MB` | ≈1.95 GiB | `zip_split.py` | Max ZIP split size |

### Gap / Action

- SpotiFLAC has **no environment-variable configuration** — everything is in a GUI config JSON.
  No config infrastructure needs porting; RubeTunes already has a richer env-var system.
- SpotiFLAC's `redownloadWithSuffix` / `linkResolver` / `allowResolverFallback` settings have
  **no equivalent env vars** in RubeTunes. These could be added as `REDOWNLOAD_WITH_SUFFIX`,
  `LINK_RESOLVER`, `ALLOW_RESOLVER_FALLBACK` if desired.

---

## 3. Spotify Auth Chain

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Pre-visit to open.spotify.com** | Only inside `SpotifyClient` (GraphQL v2 flow); not done for the ISRC/spclient token flow (`isrc_finder.go`) | **Always** — `_ensure_anon_session()` (`spotify_meta.py:312`) visits `https://open.spotify.com` to seed the `sp_t` cookie before every token refresh |
| **Server-time sync** | ❌ None — `time.Now()` (local clock) passed to `generateSpotifyTOTP` | ✅ `_fetch_spotify_server_time()` (`spotify_meta.py:343`) — `GET https://open.spotify.com/api/server-time` → `{"serverTime": N}` |
| **TOTP token request** | `GET https://open.spotify.com/api/token?reason=init&productType=web-player&totp=<code>&totpServer=<code>&totpVer=61` (no cookies required in the simple flow) (`isrc_finder.go`) | Same URL + same params, but request is made on a session that already has the `sp_t` cookie (`spotify_meta.py:425-431`) |
| **Token cache (in-memory)** | `spotifyAnonymousTokenMu sync.Mutex` + file cache (`isrc_finder.go`) | `_token_cache: dict` + `_token_lock threading.Lock()` (`spotify_meta.py:283-295,366`) |
| **Token cache (disk)** | `<appdir>/.isrc-finder-token.json` | `/tmp/tele2rub/spotify-anon-token.json` (`spotify_meta.py:370-391`) |
| **Token validity check** | `time.Now().UnixMilli() < expiresAt - 30_000` ms buffer (`isrc_finder.go`) | `expires_at > now + 30` seconds buffer (`spotify_meta.py:481,488`) |
| **Retry on failure** | ❌ None (single attempt) | ✅ 2 attempts; second attempt resets the `_anon_session` (`spotify_meta.py:499-511`) |
| **TOTP secret scraping fallback** | ❌ Not implemented | ✅ `_try_scrape_totp_secret()` (`spotify_meta.py:164`) — fetches `https://open.spotify.com`, finds JS bundle URL via regex, searches bundle for base-32 TOTP secret; cached 24 h |
| **Client-credentials fallback** | ❌ Not implemented | ✅ `_fetch_cc_token()` (`spotify_meta.py:452`) — `POST https://accounts.spotify.com/api/token` with `grant_type=client_credentials` + `SPOTIFY_CLIENT_ID`/`SECRET` |
| **SpotifyClient (GraphQL v2 session)** | `SpotifyClient` struct in `spotfetch.go` — scrapes `clientVersion` from `open.spotify.com` (base64 `appServerConfig` script tag), calls `/api/token`, then POSTs to `clienttoken.spotify.com/v1/clienttoken` | `SpotifyClient` class in `spotify_meta.py:783` — identical 3-step flow: `_get_session_info()` → `_get_access_token()` → `_get_client_token()` |
| **clientVersion scrape** | Regex on base64 `appServerConfig` JSON in `open.spotify.com` HTML (`spotfetch.go`) | Same approach: `re.search(r'<script id="appServerConfig"...>([^<]+)</script>', ...)`, then `base64.b64decode` → JSON → `clientVersion` (`spotify_meta.py:802-813`) |
| **clientToken endpoint** | `POST https://clienttoken.spotify.com/v1/clienttoken` with `client_data.client_version`, `client_id`, `js_sdk_data` (`spotfetch.go`) | Same payload and URL (`spotify_meta.py:864-875`) |
| **clientToken response check** | Checks `response_type == "RESPONSE_GRANTED_TOKEN_RESPONSE"` (`spotfetch.go`) | Same check (`spotify_meta.py:873`) |
| **429 handling** | ❌ No explicit 429 handling | ✅ Reads `Retry-After` header, sleeps, then re-raises (`spotify_meta.py:431-440`) |

### Request / Response Shape — Anonymous Token

```
GET https://open.spotify.com/api/token
  ?reason=init
  &productType=web-player
  &totp=<6-digit-code>
  &totpServer=<same-code>
  &totpVer=61

Headers (RubeTunes adds):
  Content-Type: application/json;charset=UTF-8
  Cookie: sp_t=<value>     ← requires prior visit to open.spotify.com

Response JSON:
  { "accessToken": "<bearer>",
    "accessTokenExpirationTimestampMs": <epoch-ms>,
    "isAnonymous": true,
    "clientId": "<hex>" }
```

### Gap / Action

- **Missing in SpotiFLAC (simple ISRC flow)**: `sp_t` cookie seeding, server-time sync, retry with
  session reset, TOTP scraping, client-credentials fallback. RubeTunes already has all of these.
- **Missing in RubeTunes**: None. RubeTunes is a superset.

---

## 4. TOTP Generation

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **File** | `spotbye/SpotiFLAC/backend/spotify_totp.go` | `xshayank/RubeTunes/rubetunes/spotify_meta.py:266-280` |
| **Secret (hardcoded)** | `GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY` | Same value (`spotify_meta.py:129`) |
| **Secret encoding** | Base-32 standard (decoded by `pquerna/otp` via `otpauth://` URL) | Base-32 standard: `base64.b32decode(padded_secret)` (`spotify_meta.py:272-273`) |
| **HMAC algorithm** | SHA-1 (RFC 6238 default, via `pquerna/otp`) | SHA-1: `hmac.new(key, msg, hashlib.sha1)` (`spotify_meta.py:277`) |
| **Period** | 30 seconds | 30 seconds (`counter = t // 30`) |
| **Digit count** | 6 | 6 (`% 1_000_000`, zero-padded to 6 chars) |
| **`totpVer` sent** | `61` (`spotifyTOTPVersion = 61`) | `61` (`_SPOTIFY_TOTP_VERSION = 61`, `spotify_meta.py:130`) |
| **Library vs manual** | `pquerna/otp` library (TOTP RFC 6238 compliant) | **Manual pure-Python HMAC-SHA1 truncation** — no `pyotp` or library dependency |
| **Time source** | `time.Now()` — **local system clock** | `_fetch_spotify_server_time(session)` → `int(data["serverTime"])`, falling back to `int(time.time())` if endpoint unavailable (`spotify_meta.py:274`) |
| **Counter formula** | `floor(unix_seconds / 30)` (standard, via library) | `counter = t // 30` → `struct.pack(">Q", counter)` → 8-byte big-endian |
| **Truncation** | RFC 4226 dynamic truncation (via `pquerna/otp`) | Manual: `offset = h[-1] & 0x0F; code = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF` |
| **Query params** | `totp=<code>&totpServer=<code>&totpVer=61` | `totp=<code>&totpServer=<code>&totpVer=61` (identical) |
| **Secret override** | Hardcoded only; no env var | `SPOTIFY_TOTP_SECRET` env var overrides; also live-scraped from JS bundle with 24 h TTL |

**SpotiFLAC implementation** (verbatim, `spotify_totp.go`):

```go
const spotifyTOTPSecret  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
const spotifyTOTPVersion = 61

func generateSpotifyTOTP(now time.Time) (string, int, error) {
    key, err := otp.NewKeyFromURL(fmt.Sprintf("otpauth://totp/secret?secret=%s", spotifyTOTPSecret))
    if err != nil {
        return "", 0, err
    }
    code, err := totp.GenerateCode(key.Secret(), now)
    if err != nil {
        return "", 0, err
    }
    return code, spotifyTOTPVersion, nil
}
```

**RubeTunes implementation** (`spotify_meta.py:266-280`):

```python
def _totp(secret_b32: str, server_time: int | None = None) -> str:
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    t = server_time if server_time is not None else int(time.time())
    counter = t // 30
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset: offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)
```

### Gap / Action

- RubeTunes is **more robust**: server-time sync prevents clock-skew failures, and it has a
  live-scrape fallback for when Spotify rotates the secret.
- SpotiFLAC's clock-skew risk: a host with > 15 s drift vs Spotify servers will get repeated
  token failures with no automatic recovery.
- Neither implementation needs porting from the other; RubeTunes is the superset.

---

## 5. Spotify API Surface

### Endpoints common to both

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `https://open.spotify.com/api/token` | GET | Cookie `sp_t` + TOTP params | Anonymous bearer token |
| `https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token` | GET | `Authorization: Bearer <anon>` | Internal track metadata (ISRC, album GID, cover file IDs) |
| `https://spclient.wg.spotify.com/metadata/4/album/{gid}?market=from_token` | GET | `Authorization: Bearer <anon>` | Internal album metadata (UPC) |
| `https://open.spotify.com` | GET | None | Cookie seed (`sp_t`) + `clientVersion` scrape |
| `https://clienttoken.spotify.com/v1/clienttoken` | POST | `Authorization: Bearer <anon>` | Client token for GraphQL v2 |

### SpotiFLAC-only endpoints

| Endpoint | Method | Purpose | File |
|---|---|---|---|
| `https://soundplate.com/isrc/{isrc}` | GET | ISRC fallback lookup when spclient fails | `backend/soundplate.go` (via `SongLinkClient.lookupSpotifyISRCViaSoundplate`) |

### RubeTunes-only endpoints

| Endpoint | Method | Purpose | File |
|---|---|---|---|
| `https://open.spotify.com/api/server-time` | GET | Server-time for TOTP clock sync | `spotify_meta.py:355` |
| `https://accounts.spotify.com/api/token` | POST | Client-credentials OAuth2 fallback | `spotify_meta.py:453` |
| `https://api-partner.spotify.com/pathfinder/v1/query` | GET | GraphQL v1 (anon token, persisted queries) | `spotify_meta.py:620-643` |
| `https://api-partner.spotify.com/pathfinder/v2/query` | POST | GraphQL v2 (session auth + client token) | `spotify_meta.py:886` |

### GraphQL persisted-query hashes (RubeTunes, `spotify_meta.py:621-626`)

| Operation | Hash prefix | Query params |
|---|---|---|
| `getTrack` | `612585ae...` | `uri=spotify:track:<id>` |
| `getAlbum` | `b9bfabef...` | `uri=spotify:album:<id>`, `locale`, `offset`, `limit` |
| `fetchPlaylist` | `bb67e0af...` | `uri=spotify:playlist:<id>`, `offset`, `limit`, `enableWatchFeedEntrypoint` |
| `queryArtistOverview` | `44613...` | `uri=spotify:artist:<id>`, `locale` |
| `queryArtistDiscographyAll` | `5e07d3...` | `uri=spotify:artist:<id>`, `offset`, `limit`, `order=DATE_DESC` |
| `searchDesktop` | `fcad5a3e...` | `searchTerm`, `offset`, `limit≤50`, `numberOfTopResults=5`, several boolean flags |

GraphQL requests are issued as:
```
GET https://api-partner.spotify.com/pathfinder/v1/query
  ?operationName=getTrack
  &variables={"uri":"spotify:track:<id>"}
  &extensions={"persistedQuery":{"version":1,"sha256Hash":"612585ae..."}}
Headers:
  Authorization: Bearer <anon-token>
  Accept: application/json
  User-Agent: Mozilla/5.0 ...
```

### SpotiFLAC GraphQL surface (`spotfetch.go` + `spotify_metadata.go`)

SpotiFLAC's `SpotifyClient` uses a **different endpoint and auth model**:

```
POST https://api-partner.spotify.com/pathfinder/v2/query
Headers:
  Authorization: Bearer <session-access-token>
  Client-Token: <client-token>
  Spotify-App-Version: <clientVersion>
  Content-Type: application/json
Body: JSON GraphQL query (not persisted hash — full query text)
```

The v2 endpoint is also implemented in RubeTunes' `SpotifyClient.query()` at `spotify_meta.py:882-898`.

### Cover image CDN

| Endpoint | Auth | Format |
|---|---|---|
| `https://i.scdn.co/image/{hex-file-id}` | None | JPEG; hex ID derived from spclient `file_id` field |

RubeTunes: `_spclient_file_id_to_hex()` (`spotify_meta.py:542`) — handles both raw hex and
base64-encoded file IDs.

SpotiFLAC: `backend/cover.go` — similar hex conversion from spclient metadata.

### Gap / Action

- SpotiFLAC does **not** call `api.spotify.com/v1/*` — these are explicitly forbidden per the
  RubeTunes endpoint policy (`spotify_meta.py:563-570`). Neither side uses them.
- RubeTunes' `_fetch_public_meta()` was previously `api.spotify.com/v1/tracks` but was
  rewritten to delegate to `_fetch_internal_meta()` (spclient). **Do not re-add the public REST endpoint.**
- SpotiFLAC uses `soundplate.com` as an ISRC fallback; RubeTunes does not call Soundplate
  directly — consider adding it to the resolver fallback chain.

---

## 6. Metadata Operations

| Operation | SpotiFLAC (preferred → fallback) | RubeTunes (preferred → fallback) |
|---|---|---|
| **Track info** | spclient `metadata/4/track/{gid}` → Soundplate ISRC | GraphQL `getTrack` → spclient → spclient (public REST removed) (`spotify_meta.py:555-618`) |
| **Album** | SpotifyMetadataClient GraphQL + spclient `metadata/4/album/{gid}` for UPC (`spotify_metadata.go`) | GraphQL `getAlbum` paginated (`spotify_meta.py:691-706`) |
| **Playlist** | SpotifyMetadataClient GraphQL `fetchPlaylist` paginated (`spotify_metadata.go`) | GraphQL `fetchPlaylist` paginated (`spotify_meta.py:709-724`) |
| **Search** | Not separately exposed; linked via resolver providers | GraphQL `searchDesktop` (`spotify_meta.py:761-780`) |
| **Artist profile** | SpotifyMetadataClient GraphQL `queryArtistOverview` (`spotify_metadata.go`) | GraphQL `queryArtistOverview` (`spotify_meta.py:727-740`) |
| **Artist top tracks** | Embedded in `queryArtistOverview` response (`spotify_metadata.go`) | Embedded in `queryArtistOverview` response (`spotify_meta.py:_parse_graphql_artist`) |
| **Artist albums** | SpotifyMetadataClient GraphQL `queryArtistDiscographyAll` (`spotify_metadata.go`) | GraphQL `queryArtistDiscographyAll` paginated (`spotify_meta.py:743-758`) |

### Metadata fields compared

| Field | SpotiFLAC | RubeTunes |
|---|---|---|
| Title | ✓ | ✓ |
| Artists (multi) | ✓ (configurable `Separator` in `SpotifyMetadataClient`) | ✓ (list) |
| Album | ✓ | ✓ |
| Album artist | ✓ | ✓ |
| Release date | ✓ | ✓ (ISO 8601 `YYYY-MM-DD`) |
| Track number | ✓ | ✓ |
| Disc number | ✓ | ✓ |
| ISRC | ✓ (spclient `external_id` array) | ✓ (GraphQL `externalIds.isrc` + spclient fallback) |
| UPC | ✓ (`backend/upc_tags.go` via album spclient TXXX tag) | ❌ not extracted |
| Genre | ✓ (MusicBrainz lookup optional, `backend/musicbrainz.go`) | ✓ (MusicBrainz lookup, top-3 tags joined; `resolver.py`) |
| BPM | ✓ (`backend/analysis.go` via `go-essentia`) | ❌ not implemented |
| Cover art | ✓ (`i.scdn.co` hex URL from spclient) | ✓ + optionally upgraded to 1400×1400 via Apple Music |
| Lyrics | ✓ LRCLIB → `.lrc` file (`backend/lyrics.go`) | ✓ Spotify API lyrics → embedded USLT ID3 tag |
| Duration (ms) | ✓ (used for preview validation) | ✓ (GraphQL `duration.totalMilliseconds`) |

### Gap / Action

- **UPC extraction** (`backend/upc_tags.go`): SpotiFLAC fetches the album GID from the track
  metadata, then calls spclient for the album to extract the UPC from `external_id`. RubeTunes
  does not extract UPC. Port: add a `_fetch_album_upc(album_gid)` helper to `spotify_meta.py`.
- **BPM analysis** (`backend/analysis.go`): SpotiFLAC optionally analyses downloaded audio with
  `go-essentia`. No equivalent in RubeTunes (not a priority for a bot).
- **Artist separator config**: SpotiFLAC's `Separator` field lets users choose how multiple
  artists are joined in tags. RubeTunes stores artists as a list and joins with `", "` in
  `tagging.py`. If needed, add a `ARTIST_SEPARATOR` env var.

---

## 7. ID / URI Handling

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Base-62 alphabet** | `0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ` (`isrc_finder.go:const spotifyBase62Alphabet`) | Same (`spotify_meta.py:133: _BASE62`) |
| **Base-62 → integer** | `math/big` arbitrary-precision decode (handles 22-char IDs correctly) | Pure Python: `n = n * 62 + _BASE62.index(c)` (`spotify_meta.py:202-206`) |
| **ID → GID (hex)** | `spotifyEntityIDToGID()` (`isrc_finder.go`) → `value.Text(16)` zero-padded to 32 chars | `track_id_to_gid()` (`spotify_meta.py:209-210`) → `hex(n)[2:].zfill(32)` |
| **Track URL parse** | `extractSpotifyTrackID()` — handles URI `spotify:track:<id>`, URL `open.spotify.com/track/<id>`, bare 22-char ID (`isrc_finder.go`) | `parse_spotify_track_id()` (`spotify_meta.py:213-224`) — same 3 patterns |
| **Album URL parse** | Embedded in `spotify_metadata.go` SpotifyMetadataClient | `parse_spotify_album_id()` (`spotify_meta.py`) |
| **Playlist URL parse** | Embedded in `spotify_metadata.go` | `parse_spotify_playlist_id()` (`spotify_meta.py`) |
| **Artist URL parse** | Embedded in `spotify_metadata.go` | `parse_spotify_artist_id()` (`spotify_meta.py`) |
| **Tidal ID parse** | Embedded in `tidal.go` | `parse_tidal_track_id()` (`spotify_meta.py:227-236`) |
| **Qobuz ID parse** | Embedded in `qobuz.go` | `parse_qobuz_track_id()` (`spotify_meta.py:239-251`) |
| **Amazon ASIN parse** | Embedded in `amazon.go` | `parse_amazon_track_id()` (`spotify_meta.py:254-263`) |

### Gap / Action

- Both sides implement identical algorithms. No porting needed.
- SpotiFLAC's use of `math/big` is a Go necessity for 128-bit integers; Python's native
  arbitrary-precision int handles it natively.

---

## 8. Download / Streaming Pipeline

### SpotiFLAC — orchestration in `app.go` and `backend/`

```
User submits Spotify URL
  └─► FetchTrack() → ISRC lookup (GetSpotifyTrackIdentifiersDirect)
        ├─► BoltDB ISRC cache (isrc_cache.go)
        ├─► spclient metadata/4/track/{gid} (isrc_finder.go:fetchSpotifyTrackRawData)
        └─► Soundplate fallback (soundplate.go:lookupSpotifyISRCViaSoundplate)
  └─► resolveSpotifyTrackLinks()
        ├─► linkResolver = "songlink": Deezer public API → Odesli (songlink.go)
        └─► linkResolver = "songstats": Songstats API (songstats.go)
        Both: extract Tidal + Amazon links; stop early if both found
  └─► SpotifyMetadataClient.FetchTrackMetadata() (spotify_metadata.go)
        └─► GraphQL v2 via SpotifyClient (spotfetch.go)
  └─► DownloadTrack(provider, link, quality, outputPath)
```

**Provider waterfall (SpotiFLAC, `backend/provider_priority.go`)**:

Providers are sorted by BoltDB priority score:  
`outcome_rank (success=2 > unknown=1 > failure=0)` → `LastSuccess` → `LastAttempt`.  
No formal circuit breaker — degraded providers sink to the bottom of the list naturally.

| Provider | Auth model | Quality options | Format |
|---|---|---|---|
| **Qobuz** | Scraped `app_id`+`app_secret` (HMAC-MD5 signed requests) + optional email/password | `27` (Hi-Res 24-bit) → `7` (CD FLAC 16-bit) → `6` (CD FLAC alt) (`qobuz.go`) | FLAC |
| **Tidal** | Tidal API token | `HI_RES_LOSSLESS` → `LOSSLESS` → `HIGH` (`tidal.go`) | FLAC / AAC |
| **Tidal Alt** | Proxy API `tidal.spotbye.qzz.io/get/{spotifyID}` — no auth | Single URL | FLAC |
| **Amazon Music** | Proxy API — no auth; ffmpeg decrypt post-download (`amazon.go`) | Single format | M4A (decrypted) |
| **Deezer** | Via Odesli/SongLink resolution; not a direct download provider | — | — |

**Post-download validation** (`backend/download_validation.go`):

```go
// ValidateDownloadedTrackDuration — rejects Spotify preview downloads
if expectedDuration >= 60s && actualDuration <= 35s  → reject (preview)
if expectedDuration >= 90s && (diff > 25% || diff > 15s) → reject (truncated)
```

**Tagging** (`backend/metadata.go`, 30 KB):
- MP3: `bogem/id3v2` — ID3v2.4, all standard frames
- FLAC: `go-flac/flacvorbis` — Vorbis comments
- M4A: ffmpeg post-processing (no native Go M4A tagger)

**File naming** (`backend/filename.go`):  
Template vars: `{title}`, `{artist}`, `{album}`, `{album_artist}`, `{year}`, `{date}`,
`{isrc}`, `{disc}`, `{track}`.  
Built-in presets: `artist-title`, `title`, default (`title-artist`).

**Lyrics** (`backend/lyrics.go`):  
LRCLIB chain: exact-match (`/api/get?artist_name=&track_name=&album_name=&duration=`) →
no-album match → `/api/search` → simplified title search (strip parenthetical suffixes).
Output: `.lrc` sidecar file with `[ti:]`, `[ar:]`, `[by:SpotiFlac]` headers.

### RubeTunes — `rubetunes/resolver.py` + `rubetunes/downloader.py`

```
User sends Spotify URL (rub.py → _handle_spotify_url)
  └─► resolver.get_track_info(track_id)
        ├─► LRU cache (256 entries, 10 min TTL)
        ├─► Disk ISRC cache (JSON file)
        ├─► _fetch_track_graphql (getTrack) → _parse_graphql_track
        ├─► _fetch_internal_meta (spclient) fallback
        └─► ThreadPoolExecutor (8 workers): parallel resolution
              ├─► Deezer by ISRC (_deezer_url_from_isrc)
              ├─► Qobuz by ISRC (_resolve_qobuz_by_isrc)
              ├─► Tidal by ISRC (providers/tidal.py)
              ├─► TidalAlt by Spotify ID (providers/tidal_alt.py)
              └─► Odesli/song.link (resolver.py)
        Then: Songstats fallback → Deezer ISRC fallback → MusicBrainz genre
  └─► build_platform_choices(info, quality) → ranked list
  └─► download_track(info, choice)  ← async
        └─► provider-specific download function
        └─► tagging: mutagen MP3/FLAC/M4A + ffmpeg remux fallback
```

**Provider ranking** (`downloader.py:69-160`):

| Rank | Source | Quality | Condition |
|---|---|---|---|
| 0 | `auto` (waterfall) | — | ≥2 sources available |
| 1 | Qobuz Hi-Res | `flac_hi` | `bit_depth≥24` and quality ≠ `mp3` |
| 2 | Qobuz FLAC CD | `flac_cd` | quality ≠ `mp3` |
| 3 | Tidal Alt | `flac_cd` | quality ≠ `mp3` |
| 4 | Deezer | `flac_cd` | `DEEZER_ARL` set and quality ≠ `mp3` |
| 5 | Amazon Music | `flac_cd` | quality ≠ `mp3` |
| 6 | YouTube Music | `mp3` | always last (or explicit `mp3` request) |

**Circuit-breaker filtering**: open circuits are excluded before ranking
(`downloader.py` calls `_is_circuit_open()` from `circuit_breaker.py`).

**Providers detail** (`rubetunes/providers/`):

| Provider | Auth | Search | Format |
|---|---|---|---|
| `qobuz.py` | Scraped `app_id`+`app_secret` (HMAC-MD5); optional `QOBUZ_EMAIL`/`PASSWORD` auth fallback | `track/search?isrc=` → `track/getFileUrl` | FLAC (quality 27→7→6) |
| `tidal.py` | `TIDAL_TOKEN` bearer | `tracks/byIsrc?isrc=` | FLAC / AAC |
| `tidal_alt.py` | Proxy API (4 base URLs, env-configurable `TIDAL_ALT_BASES`) | by Spotify ID or Tidal ID | FLAC; supports v2 manifest (segment concat) |
| `deezer.py` | `DEEZER_ARL` cookie | `api.deezer.com/track/isrc:{isrc}` | FLAC (Blowfish decrypt via yt-dlp) |
| `amazon.py` | Proxy API | by Amazon ASIN URL | M4A |
| `youtube.py` | yt-dlp (no auth) | `ytsearch:` | MP3 320k |
| `soundcloud.py` | yt-dlp (no auth) | direct URL | MP3 |
| `bandcamp.py` | yt-dlp (no auth) | direct URL | FLAC preferred |
| `apple_music.py` | iTunes Search API (no auth) | for cover art only | — |

**Tagging** (`rubetunes/tagging.py`):
- MP3: `mutagen.id3` — ID3v2.4, `TIT2`, `TPE1`, `TALB`, `TPE2`, `TDRC`, `TRCK`, `TPOS`, `TSRC`, `USLT` (lyrics), `APIC`
- FLAC: `mutagen.flac` — Vorbis comment block + `PICTURE` block
- M4A: `mutagen.mp4` — `©nam`, `©ART`, `©alb`, `aART`, `©day`, `trkn`, `disk`, `covr`; falls back to ffmpeg remux on mutagen write error

**File naming** (`downloader.py:_safe_name()`):  
`"{artist} - {title}"` pattern; `tagging._safe_filename()` strips OS-unsafe characters.  
No template system (unlike SpotiFLAC).

**No preview/duration validation** equivalent to SpotiFLAC's `ValidateDownloadedTrackDuration()`.

**Lyrics** (`spotify_meta.py:get_lyrics()`):  
Calls Spotify's internal lyrics API; embeds as `USLT` ID3 frame via mutagen. No LRCLIB fallback.

### Gap / Action

- **Preview detection** (`backend/download_validation.go`): port `ValidateDownloadedTrackDuration()`
  — compare `expectedMs` from GraphQL metadata with the actual downloaded file duration (via
  ffprobe or mutagen). Add to `tagging.py` or `downloader.py`.
- **Soundplate ISRC fallback**: SpotiFLAC tries `soundplate.com/isrc/{isrc}` when spclient
  fails. Port to `resolver.py` as an additional ISRC lookup step.
- **LRCLIB lyrics**: SpotiFLAC's 4-step LRCLIB chain produces `.lrc` sidecar files. RubeTunes
  only has Spotify internal lyrics. Add `_fetch_lyrics_lrclib()` as a fallback when Spotify
  lyrics API returns nothing.
- **UPC tagging**: SpotiFLAC writes UPC to a `TXXX:UPC` ID3 frame. Port to `tagging.py`.
- **File-naming templates**: SpotiFLAC supports `{isrc}`, `{disc}`, etc. in the filename.
  RubeTunes only supports `"{artist} - {title}"`. Add a `FILENAME_TEMPLATE` env var and
  expand in `tagging._safe_filename()`.
- **Qobuz proxy streams**: SpotiFLAC also uses proxy stream APIs for Qobuz
  (`qobuz.spotbye.qzz.io`, `dab.yeet.su`, `dabmusic.xyz`). RubeTunes' `qobuz.py` uses the
  official signed API path instead. Both approaches coexist; no porting required unless the
  signed API begins failing.

---

## 9. Concurrency, Queueing, Rate-Limiting & Retries

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Concurrency model** | Goroutines (one per download, managed by Wails UI) | `asyncio` + `ThreadPoolExecutor` for I/O; `BATCH_CONCURRENCY` semaphore (default 3) |
| **Download queue** | UI-controlled; no explicit server-side queue | `asyncio.Queue` in `rub.py`; queue snapshot saved on SIGTERM and restored on restart |
| **Per-user rate limit** | ❌ None (single-user desktop) | ✅ Rolling 1-hour deque (`rate_limiter.py`): `USER_TRACKS_PER_HOUR=100`; rejection message includes ETA |
| **MusicBrainz rate limit** | `time.Sleep(1100ms)` between calls, `sync.Map` in-flight dedup, 3 retries × 3 s, 5-min skip after 503 (`musicbrainz.go`) | 1.1 s sleep via `_mb_lock` threading.Lock, 60 s unavailability skip window on non-200 (`resolver.py`) |
| **Circuit breaker** | ❌ None (provider priority re-ordering only) | ✅ 3-state per `(service, provider)`: closed → open (after `CIRCUIT_FAIL_THRESHOLD` failures in `CIRCUIT_FAIL_WINDOW_SEC`) → half_open after `CIRCUIT_OPEN_DURATION_SEC` (`circuit_breaker.py:43-45`) |
| **Qobuz credentials mutex** | `qobuzCredentialsMu sync.Mutex` (`qobuz_api.go`) | `_qobuz_creds_lock threading.Lock()` (`providers/qobuz.py`) |
| **ISRC cache writes** | BoltDB (serialised by bbolt) | `_isrc_cache_lock threading.Lock()` + atomic JSON write |
| **Spotify token mutex** | `spotifyAnonymousTokenMu sync.Mutex` (`isrc_finder.go`) | `_token_lock threading.Lock()` — double-checked locking (`spotify_meta.py:483-488`) |
| **Disk guard** | ❌ None | ✅ Pre-flight check before batch: rejects if `free < MAX(MIN_FREE_SPACE_MB, multiplier × estimate)` (`disk_guard.py`) |
| **429 / rate-limit retries** | ❌ No explicit 429 handling | ✅ `_fetch_anon_token()` reads `Retry-After` header, sleeps, re-raises; circuit breaker calls `force_open()` on 429 |

### Gap / Action

- **Preview validation retries**: SpotiFLAC re-ranks providers on failure but has no
  circuit-breaker equivalent. RubeTunes' circuit breaker is strictly superior.
- **Disk guard**: port concept to any environment where disk space may be limited — already done
  in RubeTunes. No action needed.
- No changes needed: RubeTunes already implements everything SpotiFLAC does here, plus more.

---

## 10. Caching

| Cache | SpotiFLAC | RubeTunes |
|---|---|---|
| **ISRC** | BoltDB `isrc_cache.db` — no TTL, permanent (`isrc_cache.go`) | JSON `spotify-isrc-cache.json` in `/tmp/tele2rub/` — no TTL (`cache.py`) |
| **Provider priority / stats** | BoltDB `provider_priority.db` — no TTL (`provider_priority.go`) | JSON `provider_stats.json` in `/tmp/tele2rub/` — updated on each circuit state change |
| **Download history** | BoltDB `history.db` — max 10 000 entries, auto-prune 5% oldest (`history.go`) | JSON `downloads_history.json` — no size limit (`history.py`) |
| **Fetch history** | BoltDB `history.db` (separate bucket) — no TTL | — |
| **Spotify anon token** | File `<appdir>/.isrc-finder-token.json` — TTL per `expiresAt` field | File `/tmp/tele2rub/spotify-anon-token.json` — TTL per `expires_at` |
| **Qobuz credentials** | File `qobuz-api-credentials.json` — 24 h TTL (`qobuz_api.go`) | File `/tmp/tele2rub/qobuz-api-credentials.json` — 24 h TTL (`providers/qobuz.py`) |
| **Track info (LRU)** | ❌ None (re-fetches every time) | `OrderedDict` — 256 entries, 10 min TTL (`cache.py`) |
| **MusicBrainz** | `sync.Map` (in-memory, process lifetime) (`musicbrainz.go`) | Module-level dict + lock (in-memory, process lifetime) (`resolver.py`) |
| **Recent fetches** | JSON `recent_fetches.json` — manual, no TTL (`recent_fetches.go`) | — |

### Gap / Action

- **History size limit**: SpotiFLAC caps download history at 10 000 entries and prunes 500 on
  overflow. Add equivalent cap to `history.py`.
- **BoltDB vs JSON**: SpotiFLAC's BoltDB gives ACID transactional guarantees; a crash during
  JSON write in RubeTunes could corrupt the cache file. Consider wrapping JSON writes with an
  atomic rename (write to `.tmp`, then `os.replace()`). The ISRC and token caches are the most
  critical.
- **Recent fetches**: SpotiFLAC stores the last N fetched URLs (track/album/playlist) for quick
  re-fetch. No equivalent in RubeTunes; not needed for a bot.

---

## 11. Error Handling & Logging

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Logging framework** | None — `fmt.Printf` / `fmt.Println` throughout all backend files | Python `logging` — root logger `"spotify_dl"`, handlers configured in `logging_setup.py` |
| **Log format** | Unstructured text to stdout | `LOG_FORMAT=text` (default) or `LOG_FORMAT=json` (via `python-json-logger`) |
| **Log levels** | ❌ None — all output is at `Print` level | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| **Structured fields** | ❌ None | JSON mode adds `asctime`, `levelname`, `name`, `message` |
| **Sentry integration** | ❌ None | ✅ `sentry_setup.py`: `sentry_sdk.init(dsn, traces_sample_rate=0.1)`; `capture_exception(exc, user_guid, command)` |
| **Error types** | Go `error` values; formatted strings returned from functions | `DownloadError(source, message)` custom exception (`downloader.py:57-62`) |
| **Exception propagation** | Errors returned up the call stack; displayed as Wails UI events | Caught per-provider in `resolver.py`; partial results returned instead of raising |
| **Progress events** | `runtime.EventsEmit(ctx, "download:progress", ...)` — Wails IPC | Rubika message edits (rub.py) |
| **Preview validation errors** | Typed returns from `ValidateDownloadedTrackDuration()` | ❌ Not implemented |
| **Rate-limit log** | ❌ None | `log.warning("Spotify /api/token returned 429 — sleeping %ds ...")` |

### Gap / Action

- SpotiFLAC has no structured logging or error telemetry. RubeTunes is the superset.
- Add Sentry `capture_exception` call in the download pipeline when `DownloadError` is raised
  with a non-transient error (already partially done in `rub.py`).

---

## 12. HTTP Routes / Handlers Exposed

### SpotiFLAC — Wails RPC bindings (`app.go`)

All frontend-callable functions are methods on the `App` struct, bound via `app.Bind(&app)`.
There is no HTTP server — communication is IPC through the Wails runtime.

| Function | Purpose |
|---|---|
| `FetchTrack(spotifyURL string)` | Full ISRC lookup + metadata + platform links |
| `FetchAlbum(spotifyURL string)` | Album tracks listing via GraphQL |
| `FetchPlaylist(spotifyURL string)` | Playlist tracks listing via GraphQL |
| `DownloadTrack(provider, link, quality, outputPath, ...)` | Single track download with provider selection |
| `GetCurrentIP()` | IP geolocation for region detection |
| `GetConfig() / SetConfig*()` | JSON config CRUD (linkResolver, redownloadWithSuffix, etc.) |
| `GetHistory() / ClearHistory()` | Download history management |
| `GetRecentFetches() / SaveRecentFetches()` | Recent URL history |
| `GetProviderPriority() / UpdateProviderPriority()` | Provider priority management |
| `GetLyrics(artist, title, album, duration)` | LRCLIB lyrics fetch |
| `OpenFolder(path)` | Open download directory in system file manager |

Progress is emitted via `runtime.EventsEmit(ctx, "download:progress", progressEvent)`.

### RubeTunes — Rubika command handlers (`rub.py`)

Commands are matched by regex on incoming Rubika messages:

| Command | Pattern match | Purpose |
|---|---|---|
| `!spotify <url>` | Spotify track/album/playlist URL | Download with optional format hint (`mp3`, `flac`, `hires`) |
| `!tidal <url>` | Tidal URL | Tidal Alt download |
| `!qobuz <url>` | Qobuz URL | Direct Qobuz download |
| `!amazon <url>` | Amazon Music URL | Amazon download |
| `!soundcloud <url>` | SoundCloud URL | yt-dlp download |
| `!bandcamp <url>` | Bandcamp URL | yt-dlp FLAC download |
| `!queue` | — | Show current queue depth and items |
| `!history` | — | List recent downloads |
| `!help` | — | Command list |
| `!admin whitelist <on/off>` | admin only | Toggle whitelist enforcement |
| `!admin ban/unban <guid>` | admin only | Ban/unban a user |
| `!admin stats` | admin only | Provider circuit-breaker stats |
| `!admin reset <provider>` | admin only | Reset a circuit breaker |

Admin commands gated by `GUID ∈ ADMIN_GUIDS` env var.

### Gap / Action

- SpotiFLAC has no concept of commands or admin controls — it's a single-user GUI.
- RubeTunes has no equivalent to `GetCurrentIP()` (geolocation); this is for Wails UI only.
- RubeTunes has no `/health` or `/version` HTTP endpoint (only Prometheus metrics on 9090).

---

## 13. Models / Schemas

### SpotiFLAC key structs (`backend/`)

```go
// isrc_cache.go — BoltDB ISRC cache entry
type isrcCacheEntry struct {
    TrackID   string    `json:"track_id"`
    ISRC      string    `json:"isrc"`
    UpdatedAt time.Time `json:"updated_at"`
}

// provider_priority.go — BoltDB provider success/failure tracking
type providerPriorityEntry struct {
    Service      string    `json:"service"`
    Provider     string    `json:"provider"`
    LastOutcome  string    `json:"last_outcome"`  // "success"|"failure"|""
    LastAttempt  time.Time `json:"last_attempt"`
    LastSuccess  time.Time `json:"last_success"`
    LastFailure  time.Time `json:"last_failure"`
    SuccessCount int       `json:"success_count"`
    FailureCount int       `json:"failure_count"`
}

// history.go — download history entry
type HistoryItem struct {
    SpotifyID string    `json:"spotify_id"`
    Title     string    `json:"title"`
    Artists   []string  `json:"artists"`
    Quality   string    `json:"quality"`
    Format    string    `json:"format"`
    Path      string    `json:"path"`
    Source    string    `json:"source"`
    Timestamp time.Time `json:"timestamp"`
}

// isrc_finder.go — ISRC + UPC identifiers
type SpotifyTrackIdentifiers struct {
    ISRC string `json:"isrc,omitempty"`
    UPC  string `json:"upc,omitempty"`
}

// recent_fetches.go — recent search history
type RecentFetchItem struct {
    ID        string `json:"id"`
    URL       string `json:"url"`
    Type      string `json:"type"`  // track|album|playlist
    Name      string `json:"name"`
    Artist    string `json:"artist"`
    Image     string `json:"image"`
    Timestamp int64  `json:"timestamp"`
}

// SpotifyClient (spotfetch.go)
type SpotifyClient struct {
    client        *http.Client
    accessToken   string
    clientToken   string
    clientID      string
    deviceID      string
    clientVersion string
    cookies       map[string]string
}
```

### RubeTunes key schemas (plain dicts / dataclasses)

```python
# Track info dict (resolver.py _resolve_all_platforms output)
{
    "track_id":         str,
    "title":            str,
    "artists":          list[str],
    "album":            str,
    "album_artist":     str,
    "release_date":     str,          # ISO date YYYY-MM-DD or YYYY
    "cover_url":        str,
    "track_number":     int,
    "disc_number":      int,
    "isrc":             str | None,
    "genre":            str,
    # Qobuz
    "qobuz_id":         str | None,
    "qobuz_url":        str | None,
    "qobuz_bit_depth":  int,          # 16 or 24
    "qobuz_sample_rate": int,         # 44100 or 96000 / 192000
    # Deezer
    "deezer_id":        str | None,
    "deezer_url":       str | None,
    # Tidal
    "tidal_id":         str | None,
    "tidal_url":        str | None,
    # Tidal Alt
    "tidal_alt_url":    str | dict | None,  # str for simple URL; dict for v2 manifest
    # Amazon
    "amazon_url":       str | None,
}

# Platform choice entry (downloader.py build_platform_choices)
{
    "source":   str,    # qobuz|tidal_alt|deezer|amazon|youtube|auto
    "quality":  str,    # mp3|flac_cd|flac_hi
    "label":    str,    # human-readable string
    "url":      str | None,
    "rank":     int,    # 0=auto waterfall, 1–6 by priority
}

# Download history entry (history.py)
{
    "file":       str,
    "user_guid":  str,
    "timestamp":  str,          # ISO 8601 datetime
    "title":      str,
    "artists":    list[str],
    "source":     str,
    "quality":    str,
}

# Circuit breaker state (circuit_breaker.py)
{
    "state":        str,            # closed|open|half_open
    "failures":     list[float],    # epoch timestamps of recent failures
    "last_failure": float,
    "opened_at":    float | None,
}
```

### Gap / Action

- RubeTunes uses plain `dict`s throughout. No Pydantic models or dataclasses are used.
  Adding Pydantic for `TrackInfo`, `PlatformChoice`, `DownloadHistoryEntry` would improve type
  safety but is a refactoring task, not a SpotiFLAC port requirement.
- SpotiFLAC's `SpotifyTrackIdentifiers` explicitly tracks UPC alongside ISRC; RubeTunes
  discards UPC. Add a `upc` field to the track info dict and populate it from the album spclient
  call.

---

## 14. Dependencies

### SpotiFLAC — `spotbye/SpotiFLAC/go.mod` (Go 1.26)

| Package | Version | Purpose |
|---|---|---|
| `github.com/wailsapp/wails/v2` | `v2.11.0` | Desktop GUI framework |
| `github.com/pquerna/otp` | latest | TOTP generation (RFC 6238) |
| `go.etcd.io/bbolt` | latest | BoltDB embedded KV store (caches, history) |
| `github.com/bogem/id3v2` | latest | MP3 ID3v2 tagging |
| `github.com/go-flac/flacvorbis` | latest | FLAC Vorbis comment writing |
| `github.com/go-flac/go-flac` | latest | FLAC block I/O |
| `math/big` | stdlib | Base-62 → big-integer conversion |
| `net/http` | stdlib | HTTP client |
| `encoding/json` | stdlib | JSON parsing |
| `golang.org/x/...` | stdlib | Sync primitives |

### RubeTunes — `xshayank/RubeTunes/requirements.txt`

| Package | Version pin | Purpose |
|---|---|---|
| `pyrogram` | `==2.0.106` | Telegram MTProto (legacy, appears unused) |
| `tgcrypto` | `==1.2.5` | Crypto for pyrogram |
| `rubpy` | `==7.3.4` | Rubika messenger client |
| `python-dotenv` | `==1.2.2` | `.env` file loading |
| `requests` | `>=2.30.0` | HTTP client |
| `mutagen` | `==1.47.0` | Audio metadata (MP3/FLAC/M4A) |
| `python-json-logger` | `>=2.0` | Structured JSON logging |
| `prometheus-client` | `>=0.20` | Prometheus metrics exposition |
| `sentry-sdk` | `>=2.0` | Error reporting |

### `requirements-dev.txt` (RubeTunes)

`pytest`, `responses`, `pytest-asyncio`, `pre-commit`, `black`, `ruff`, `mypy`.

### Gap / Action

- `pyrogram==2.0.106` and `tgcrypto==1.2.5` appear unused (Rubika uses `rubpy`). These should
  be removed to reduce attack surface and image size.
- SpotiFLAC uses `pquerna/otp` for TOTP; RubeTunes implements it manually — **do not add pyotp**
  since the manual implementation is already verified against SpotiFLAC's output.
- SpotiFLAC uses BoltDB (`go.etcd.io/bbolt`). If RubeTunes needs crash-safe caches, consider
  `lmdb` or `sqlite3` (stdlib). Not required unless JSON cache corruption becomes a problem.
- SpotiFLAC uses `bogem/id3v2` and `go-flac/flacvorbis` for tagging; RubeTunes uses `mutagen`
  which supports the same formats plus M4A. No change needed.

---

## 15. Docker / Runtime

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Containerised** | ❌ No Dockerfile — native desktop binary | ✅ `xshayank/RubeTunes/Dockerfile` |
| **Base image** | n/a | `python:3.11-slim` |
| **System packages** | Bundled / OS-provided WebView2 (Windows), WebKit (Linux/macOS), optional ffmpeg | `ffmpeg` + `curl` (apt) |
| **yt-dlp** | ❌ Not used | Installed from GitHub releases (`/usr/local/bin/yt-dlp`) |
| **Working directory** | n/a | `/app` |
| **Exposed ports** | None | `9090` (Prometheus metrics) |
| **Entrypoint / CMD** | Native binary or `wails dev` | `CMD ["python", "main.py"]` |
| **Healthcheck** | OS process health | ❌ Not defined in Dockerfile |
| **Volumes** | n/a | `/app/downloads`, `/app/state.json`, `/app/downloads_history.json` |
| **Env defaults** | n/a | `ENV LOG_FORMAT=json`, `ENV METRICS_PORT=9090` |
| **docker-compose** | n/a | `docker-compose.yml` — single service `bot`, named volumes `downloads` + `state`, bind-mounts for `state.json` and `downloads_history.json`, port `9090:9090` |
| **Graceful shutdown** | OS signal → process exit | `main.py` SIGTERM handler → forward to child → wait 30 s → SIGKILL |

### Gap / Action

- Add a `HEALTHCHECK` to `Dockerfile` (e.g., probe Prometheus endpoint or check child process):
  ```dockerfile
  HEALTHCHECK --interval=30s --timeout=10s CMD curl -sf http://localhost:9090/metrics || exit 1
  ```
- Consider pinning the `yt-dlp` version in the Dockerfile instead of using `latest` to ensure
  reproducible builds.

---

## 16. Tests

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **Test files** | ❌ None | ✅ `tests/test_spotify_dl.py`, `tests/test_spotify_pathfinder.py`, `tests/test_new_modules.py`, `tests/test_regressions.py` |
| **CI / CD** | ❌ No GitHub Actions workflows (only `.github/FUNDING.yml` + issue templates) | ❌ No CI workflows |
| **Framework** | n/a | `pytest` + `responses` (HTTP mocking) + `pytest-asyncio` |
| **TOTP tests** | ❌ None | ✅ Tests for `_totp()` against known timestamp in `test_spotify_dl.py` |
| **GraphQL tests** | ❌ None | ✅ `test_spotify_pathfinder.py` mocks `api-partner.spotify.com` |
| **Download pipeline tests** | ❌ None | ✅ `test_spotify_dl.py` covers provider waterfall, circuit breaker, rate limiter |
| **Regression tests** | ❌ None | ✅ `test_regressions.py` covers CHANGELOG items R1–R10 |
| **New module tests** | ❌ None | ✅ `test_new_modules.py` covers `rate_limiter`, `disk_guard`, `apple_music` |
| **Monkey-patching** | n/a | `sys.modules` substitution to patch `time.time()` via `spotify_dl.time` |
| **Linting** | ❌ No lint config | `ruff` (E/F/I/UP/B rules), `black` (line-length=100), `mypy` (3.11, non-strict) |
| **Pre-commit hooks** | ❌ None | `black` + `ruff` + standard file fixers (`.pre-commit-config.yaml`) |

### Gap / Action

- Add GitHub Actions workflow (`.github/workflows/ci.yml`) to run `pytest` on every push/PR.
- Consider adding a test for the `SpotifyClient` class (`spotify_meta.py:783`) — specifically
  the clienttoken endpoint integration.
- Add test for `_try_scrape_totp_secret()` with a mocked `open.spotify.com` HTML response.

---

## 17. Security / Secrets Handling

| Aspect | SpotiFLAC | RubeTunes |
|---|---|---|
| **TOTP secret in source** | ✅ Hardcoded constant in `spotify_totp.go:12` | ✅ Hardcoded constant in `spotify_meta.py:129`; overridable via `SPOTIFY_TOTP_SECRET` env var |
| **Qobuz fallback credentials** | ✅ Hardcoded `app_id="712109809"`, `app_secret="589be88e4538daea11f509d29e4a23b1"` in `qobuz_api.go` (labeled "embedded-default") | ✅ Same defaults in `providers/qobuz.py:56-57` (`_QOBUZ_DEFAULT_APP_ID`, `_QOBUZ_DEFAULT_APP_SECRET`) |
| **Proxy trust** | Unconditionally trusts `*.spotbye.qzz.io`, `afkar.xyz` proxies (no TLS pinning, no signature verification) | Same unconditional trust |
| **Deezer ARL** | ❌ No Deezer integration | Via `DEEZER_ARL` env var; never logged |
| **Qobuz credentials** | Scraped or hardcoded; stored in `qobuz-api-credentials.json` (0644 perms) | Scraped or env var; stored in `/tmp/tele2rub/qobuz-api-credentials.json` |
| **Sentry PII** | ❌ No Sentry | `user_guid` (Rubika GUID) and `command` attached as tags; no usernames, phone, or message content |
| **Log redaction** | No logging framework; no redaction possible | Token values never logged; `log.debug` shows `sp_t=(none)` placeholder |
| **Token file permissions** | `os.WriteFile(..., 0o644)` | No explicit permission (inherits process umask) |
| **TOTP clock-skew** | **Vulnerable**: uses `time.Now()` — clock drift > 15 s causes repeated token failures | **Mitigated**: syncs to Spotify server time before each token request |
| **Admin auth (bot)** | ❌ N/A | GUID membership in `ADMIN_GUIDS` env var; no cryptographic verification — trust is Rubika-platform-provided |
| **Download validation** | `ValidateDownloadedTrackDuration()` detects and rejects Spotify preview downloads | ❌ Not implemented — could serve a 30 s preview silently |
| **Input validation** | `net/url` URL parsing; bare track IDs validated by length=22 | Compiled regex patterns in `rub.py`; format hints stripped before processing |
| **File permissions** | `0o644` for all written files | Default umask |

### Gap / Action

- **Token file permissions**: explicitly set `/tmp/tele2rub/spotify-anon-token.json` to `0o600`
  to prevent other processes from reading the cached bearer token.
- **Preview protection**: add `ValidateDownloadedTrackDuration()` equivalent to `downloader.py`
  to avoid silently delivering 30 s Spotify previews to users.
- **Atomic JSON cache writes**: use `os.replace()` (atomic rename) instead of direct
  `Path.write_text()` to prevent corruption if the process crashes mid-write.

---

## 18. Miscellaneous

### Qobuz credential scraping

Both projects scrape `open.qobuz.com` to obtain `app_id` and `app_secret`. Algorithm is
identical in both:

1. `GET https://open.qobuz.com/track/1`
2. Regex `<script src="(/resources/.../js/main.js)">` to find bundle URL
3. Download bundle; regex for `app_id:"(\d{9})"` and `app_secret:"([a-f0-9]{32})"`
4. Probe credentials against a known ISRC (`USUM71703861`) to verify they work
5. Fall back to hardcoded `app_id=712109809` / `app_secret=589be88e4538daea11f509d29e4a23b1`
6. Cache to `qobuz-api-credentials.json` for 24 h

SpotiFLAC: `backend/qobuz_api.go`; mutex-protected; auto-refresh on 400/401.  
RubeTunes: `rubetunes/providers/qobuz.py:_scrape_qobuz_open_credentials()`; `_qobuz_creds_lock`; auto-refresh.

### Tidal Alt — two implementations

| Aspect | SpotiFLAC (`backend/tidal_alt.go`) | RubeTunes (`rubetunes/providers/tidal_alt.py`) |
|---|---|---|
| **Proxy base URLs** | 1 hardcoded: `tidal.spotbye.qzz.io/get/{spotifyID}` | 4 defaults; overridable via `TIDAL_ALT_BASES` env var |
| **Response format** | `{"title": str, "link": str}` — simple URL | v1: same simple URL; v2: base64-encoded manifest JSON → `{urls, codecs, mimeType}` → segment concatenation |
| **Lookup by Tidal ID** | ❌ Not supported | ✅ `_get_tidal_alt_url_by_tidal_id(tidal_id)` |
| **Per-base timeout** | Implicit HTTP client timeout | `_TIDAL_ALT_TIMEOUT=8` s per base |

### History size limit

SpotiFLAC (`history.go`): caps `DownloadHistory` at 10 000 entries; auto-prunes 500 (5%)
oldest when limit is reached.

RubeTunes (`history.py`): no size limit on `downloads_history.json`. Add cap with pruning.

### Prometheus metrics (RubeTunes only)

`rubetunes/metrics.py` exposes on `METRICS_PORT` (default 9090):

```
rubetunes_downloads_total{source, status}              # Counter
rubetunes_provider_failures_total{provider, reason}    # Counter
rubetunes_resolutions_total{provider, outcome}         # Counter
rubetunes_queue_depth                                  # Gauge
rubetunes_circuit_open{provider}                       # Gauge
rubetunes_download_duration_seconds                    # Histogram
rubetunes_resolution_duration_seconds                  # Histogram
```

SpotiFLAC has no monitoring. No porting required.

### Cover art upgrade (RubeTunes only)

`rubetunes/providers/deezer.py:_upgrade_spotify_cover_url()` upgrades Spotify CDN image hashes
from 300px or 640px to max-resolution (`ab67616d000082c1`).

`rubetunes/providers/apple_music.py` queries iTunes Search API for 1400×1400 JPEG cover art
and replaces the Spotify cover URL.

SpotiFLAC uses the cover URL as returned by Spotify (typically 640px). No equivalent upgrade.

### Apple Music / SoundCloud / Bandcamp (RubeTunes only)

- `rubetunes/providers/apple_music.py` — iTunes Search API for cover art enrichment
- `rubetunes/providers/soundcloud.py` — yt-dlp MP3 download
- `rubetunes/providers/bandcamp.py` — yt-dlp FLAC-preferred download

None of these exist in SpotiFLAC. Soundcloud and Bandcamp are bot-specific.

### File-naming templates (SpotiFLAC only)

`backend/filename.go`: configurable template with `{title}`, `{artist}`, `{album}`,
`{album_artist}`, `{year}`, `{date}`, `{isrc}`, `{disc}`, `{track}`. Three built-in presets.

RubeTunes: fixed `"{artist} - {title}"` pattern. Add `FILENAME_TEMPLATE` env var if needed.

### Songstats link resolver (SpotiFLAC, `backend/songstats.go`)

Alternative to Odesli/SongLink — uses `songstats.com` API to resolve Spotify → Tidal + Amazon
links. Configurable via `linkResolver="songstats"` setting. SpotiFLAC tries this as an
alternative to the default Deezer→Odesli chain.

RubeTunes: `resolver.py` calls Odesli primarily; Songstats is a secondary source.

### BPM analysis (SpotiFLAC only, `backend/analysis.go`)

Analyses downloaded audio using `go-essentia` to extract BPM, writes to `TBPM` ID3 frame.
No equivalent in RubeTunes.

### Version history

- **SpotiFLAC**: `wails.json productVersion: "7.1.5"` — no public CHANGELOG.
- **RubeTunes**: `CHANGELOG.md` documents v1.0.0 (SpotiFLAC parity port), v1.1.0 (UX &
  resilience), v2.0.0 (Mega Improvement PR: features A1–E1, regressions R1–R10).

### GitHub / CI (SpotiFLAC)

`.github/` contains only `FUNDING.yml` and `ISSUE_TEMPLATE/`. No Actions workflows. No CI.

---

## Port Checklist

The following is an ordered, actionable list of every change RubeTunes needs to match or exceed
SpotiFLAC's backend behaviour, organised by area.

### Auth

- [ ] **AUTH-1** Add `SPOTIFY_TOTP_SECRET` env-var override documentation to `.env.example`
  (already implemented in `spotify_meta.py:156`; just document it).
- [ ] **AUTH-2** Set token cache file permissions to `0o600` in `_save_spotify_token()`
  (`spotify_meta.py:385`).

### TOTP

- [ ] **TOTP-1** Add a unit test that verifies `_totp(secret, server_time=1700000000)` produces
  the same 6-digit code as SpotiFLAC's `generateSpotifyTOTP` with the same `now` value.

### ID / URI

- *(No gaps — both sides have identical implementations.)*

### GraphQL / Spotify API

- [ ] **GQL-1** Add `_fetch_album_upc(album_gid)` helper to `spotify_meta.py` that calls
  `spclient.wg.spotify.com/metadata/4/album/{gid}` and extracts `UPC` from `external_id`.
- [ ] **GQL-2** Expose UPC in the track info dict (`resolver.py`) and write it to `TXXX:UPC`
  ID3 frame in `tagging.py`.
- [ ] **GQL-3** Add Soundplate ISRC fallback: when spclient metadata fails, try
  `soundplate.com/isrc/{isrc}` (port `backend/soundplate.go` logic to `resolver.py`).

### Providers / Downloads

- [ ] **DL-1** Implement preview/duration validation: after download, compare
  `duration_ms` from track metadata with actual file duration (ffprobe or mutagen). Reject and
  mark DownloadError if `expected ≥ 60 s` and `actual ≤ 35 s`.
- [ ] **DL-2** Add LRCLIB lyrics fallback to `spotify_meta.py:get_lyrics()`: when Spotify
  internal lyrics API returns nothing, try LRCLIB 4-step chain (port `backend/lyrics.go`).
- [ ] **DL-3** Add `FILENAME_TEMPLATE` env var with `{title}`, `{artist}`, `{album}`,
  `{year}`, `{disc}`, `{track}`, `{isrc}` substitutions in `tagging._safe_filename()`.
- [ ] **DL-4** Remove unused `pyrogram==2.0.106` and `tgcrypto==1.2.5` from `requirements.txt`.
- [ ] **DL-5** Pin yt-dlp version in `Dockerfile` instead of using `latest`.

### Routes / Bot

- [ ] **ROUTE-1** Add `!admin lrclib <on/off>` command to toggle LRCLIB lyrics (optional, low
  priority).

### Dependencies

- [ ] **DEP-1** Remove `pyrogram` and `tgcrypto` from `requirements.txt` (see DL-4).

### Docker

- [ ] **DOCKER-1** Add `HEALTHCHECK` to `Dockerfile`:
  ```dockerfile
  HEALTHCHECK --interval=30s --timeout=10s CMD curl -sf http://localhost:${METRICS_PORT:-9090}/metrics || exit 1
  ```

### Tests

- [ ] **TEST-1** Add unit test verifying TOTP output matches SpotiFLAC at a fixed timestamp
  (e.g., `server_time=1700000000`).
- [ ] **TEST-2** Add unit test for `_fetch_album_upc()` mocking spclient album response.
- [ ] **TEST-3** Add unit test for preview detection / `ValidateDownloadedTrackDuration()`
  equivalent.
- [ ] **TEST-4** Add unit test for LRCLIB fallback with mocked HTTP responses.
- [ ] **TEST-5** Add GitHub Actions workflow (`.github/workflows/ci.yml`) running `pytest` on
  push and PR.

### Caching

- [ ] **CACHE-1** Cap `downloads_history.json` at 10 000 entries; auto-prune 500 oldest
  (`history.py`), matching SpotiFLAC `history.go`.
- [ ] **CACHE-2** Use atomic writes (write to `.tmp` then `os.replace()`) for
  `spotify-anon-token.json`, `spotify-isrc-cache.json`, and `qobuz-api-credentials.json`.

### Security

- [ ] **SEC-1** Set `spotify-anon-token.json` permissions to `0o600` (see AUTH-2).
- [ ] **SEC-2** Implement preview detection (see DL-1) to prevent silently delivering Spotify
  30 s previews.

---

*Generated on 2026-04-25 from SpotiFLAC commit `30cbcf8` and RubeTunes HEAD.*
