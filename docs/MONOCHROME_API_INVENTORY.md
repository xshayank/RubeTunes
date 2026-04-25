# Monochrome API Inventory

This document lists every API endpoint discovered in
[monochrome-music/monochrome](https://github.com/monochrome-music/monochrome)
that has been ported into `rubetunes/providers/monochrome/`.

File:line citations point into the upstream repository at commit
`3347a2ea6d91eb472de8a7a6968301c957327fd8`.

---

## Authentication

### POST `https://auth.tidal.com/v1/oauth2/token` — Client Credentials

| Field | Value |
|-------|-------|
| **Method** | POST |
| **URL** | `https://auth.tidal.com/v1/oauth2/token` |
| **Headers** | `Content-Type: application/x-www-form-urlencoded` <br>`Authorization: Basic base64(<client_id>:<client_secret>)` |
| **Body** | `client_id=txNoH4kkV41MfH25&client_secret=<secret>&grant_type=client_credentials` |
| **Response fields consumed** | `access_token` (bearer token), `expires_in` (seconds) |
| **Upstream citation** | [`functions/track/[id].js#L11-L30`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L11-L30) |

**Constants used:**
- `CLIENT_ID = "txNoH4kkV41MfH25"` — [`functions/track/[id].js#L7`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L7)
- `CLIENT_SECRET = "dQjy0MinCEvxi1O4UmxvxWnDjt4cgHBPw8ll6nYBk98="` — [`functions/track/[id].js#L8`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L8)

---

## Direct Tidal API Endpoints

These are called by the frontend's `TidalAPI` class as a primary source,
before falling back to proxy instances.

### GET `https://api.tidal.com/v1/tracks/{id}/` — Track Metadata

| Field | Value |
|-------|-------|
| **Method** | GET |
| **URL** | `https://api.tidal.com/v1/tracks/{id}/` |
| **Query** | `countryCode=US` |
| **Headers** | `Authorization: Bearer <access_token>` |
| **Response fields consumed** | `id`, `title`, `version`, `duration`, `artists`, `artist`, `album` (id, title, cover), `trackNumber`, `volumeNumber`, `isrc`, `audioQuality`, `audioModes`, `copyright`, `url`, `previewUrl` |
| **Upstream citation** | [`functions/track/[id].js#L48-L55`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L48-L55) |

### GET `https://api.tidal.com/v1/tracks/{id}/playbackinfo` — Stream URL

| Field | Value |
|-------|-------|
| **Method** | GET |
| **URL** | `https://api.tidal.com/v1/tracks/{id}/playbackinfo` |
| **Query** | `audioquality=LOW`, `playbackmode=STREAM`, `assetpresentation=FULL`, `countryCode=US` |
| **Headers** | `Authorization: Bearer <access_token>` |
| **Response fields consumed** | `url`, `streamUrl`, `audioQuality`, `manifest`, `manifestMimeType` |
| **Upstream citation** | [`functions/track/[id].js#L57-L66`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L57-L66) |

### GET `https://api.tidal.com/v1/albums/{id}` — Album Metadata

| Field | Value |
|-------|-------|
| **Method** | GET |
| **URL** | `https://api.tidal.com/v1/albums/{id}` |
| **Query** | `countryCode=US` |
| **Headers** | `Authorization: Bearer <access_token>` |
| **Response fields consumed** | `id`, `title`, `artist`, `artists`, `numberOfTracks`, `releaseDate`, `cover`, `audioQuality`, `copyright`, `upc`, `type` |
| **Upstream citation** | [`functions/album/[id].js#L42-L48`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/album/%5Bid%5D.js#L42-L48) |

### GET `https://api.tidal.com/v1/artists/{id}` — Artist Metadata

| Field | Value |
|-------|-------|
| **Method** | GET |
| **URL** | `https://api.tidal.com/v1/artists/{id}` |
| **Query** | `countryCode=US` |
| **Headers** | `Authorization: Bearer <access_token>` |
| **Response fields consumed** | `id`, `name`, `artistTypes`, `url`, `picture`, `popularity` |
| **Upstream citation** | [`functions/artist/[id].js#L42-L48`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/artist/%5Bid%5D.js#L42-L48) |

### GET `https://api.tidal.com/v1/playlists/{id}` — Playlist Metadata

| Field | Value |
|-------|-------|
| **Method** | GET |
| **URL** | `https://api.tidal.com/v1/playlists/{id}` |
| **Query** | `countryCode=US` |
| **Headers** | `Authorization: Bearer <access_token>` |
| **Response fields consumed** | `uuid`, `title`, `numberOfTracks`, `squareImage`, `image`, `duration` |
| **Upstream citation** | [`functions/playlist/[id].js#L42-L48`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/playlist/%5Bid%5D.js#L42-L48) |

---

## Proxy / HiFi API Instances

The frontend routes below are served by community-run Tidal HiFi proxy
instances (see `DEFAULT_PROXY_INSTANCES` in `constants.py`).  The instance
list is refreshed dynamically from:

```
GET https://tidal-uptime.geeked.wtf
```

Response: `{ "api": [ { "url": "https://..." }, ... ] }`

**Source:** [`functions/track/[id].js#L64-L91`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L64-L91)

Default fallback instances (used when uptime endpoint is unreachable):

| Instance URL | Source |
|---|---|
| `https://eu-central.monochrome.tf` | [`functions/track/[id].js#L80`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L80) |
| `https://us-west.monochrome.tf` | [`functions/track/[id].js#L81`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L81) |
| `https://arran.monochrome.tf` | [`functions/track/[id].js#L82`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L82) |
| `https://triton.squid.wtf` | [`functions/track/[id].js#L83`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L83) |
| `https://api.monochrome.tf` | [`functions/track/[id].js#L84`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L84) |
| `https://monochrome-api.samidy.com` | [`functions/track/[id].js#L85`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L85) |
| `https://maus.qqdl.site` | [`functions/track/[id].js#L86`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L86) |
| `https://vogel.qqdl.site` | [`functions/track/[id].js#L87`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L87) |
| `https://katze.qqdl.site` | [`functions/track/[id].js#L88`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L88) |
| `https://hund.qqdl.site` | [`functions/track/[id].js#L89`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L89) |
| `https://tidal.kinoplus.online` | [`functions/track/[id].js#L90`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L90) |
| `https://wolf.qqdl.site` | [`functions/track/[id].js#L91`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L91) |

---

## Proxy API Routes

All routes below are relative to each proxy instance base URL.

### Search

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /search/?q=<query>` | `q` | Combined search (tracks, albums, artists, playlists, videos) | [`js/api.js#L205-L255`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L205) |
| `GET /search/?s=<query>` | `s` | Track-only search | [`js/api.js#L257-L280`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L257) |
| `GET /search/?a=<query>` | `a` | Artist search | [`js/api.js#L282-L302`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L282) |
| `GET /search/?al=<query>` | `al` | Album search | [`js/api.js#L304-L325`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L304) |
| `GET /search/?p=<query>` | `p` | Playlist search | [`js/api.js#L327-L345`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L327) |
| `GET /search/?v=<query>` | `v` | Video search | [`js/api.js#L347-L365`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L347) |

**Response shape** (combined search):
```json
{
  "tracks":    { "items": [...], "totalNumberOfItems": N, "limit": N, "offset": N },
  "albums":    { "items": [...], ... },
  "artists":   { "items": [...], ... },
  "playlists": { "items": [...], ... },
  "videos":    { "items": [...], ... }
}
```

### Track

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /info/?id=<id>` | `id` | Full track metadata | [`js/api.js#L500-L520`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L500) |
| `GET /trackManifests/?id=N&quality=Q&formats=F&adaptive=false` | `id`, `quality`, `formats` (multi), `adaptive` | Track streaming manifest (new OpenAPI route) | [`js/api.js#L530-L555`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L530) |
| `GET /stream?id=<id>&quality=<Q>` | `id`, `quality` | Legacy stream URL endpoint | [`functions/track/[id].js#L102-L107`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L102) |
| `GET /recommendations/?id=<id>` | `id` | Track recommendations | [`js/api.js#L522-L530`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L522) |
| `GET /video/?id=<id>` | `id` | Video metadata + manifest | [`js/api.js#L370-L390`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L370) |

**Track manifest quality tokens and formats:**

| Quality token | Format string | Audio | Upstream citation |
|---------------|--------------|-------|-------------------|
| `HI_RES_LOSSLESS` | `FLAC_HIRES` | 24-bit FLAC | [`js/api.js#L397-L409`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L397) |
| `LOSSLESS` | `FLAC` | 16-bit FLAC | [`js/api.js#L397-L409`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L397) |
| `HIGH` | `AACLC` | AAC LC | [`js/api.js#L397-L409`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L397) |
| `LOW` | `HEAACV1` | HE-AAC v1 | [`js/api.js#L397-L409`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L397) |
| `DOLBY_ATMOS` | `EAC3_JOC` | E-AC-3 JOC | [`js/api.js#L397-L409`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L397) |

### Album

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /album/?id=<id>` | `id` | Album metadata + track list (first page) | [`js/api.js#L340-L430`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L340) |
| `GET /album/?id=<id>&offset=N&limit=500` | `id`, `offset`, `limit` | Paginated track list | [`js/api.js#L400-L420`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L400) |
| `GET /album/similar/?id=<id>` | `id` | Similar albums | [`js/api.js#L582-L600`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L582) |

### Playlist

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /playlist/?id=<id>` | `id` | Playlist metadata + track list | [`js/api.js#L432-L498`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L432) |
| `GET /playlist/?id=<id>&offset=N` | `id`, `offset` | Paginated track list | [`js/api.js#L460-L480`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L460) |

### Artist

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /artist/?id=<id>` | `id` | Artist profile + discography | [`js/api.js#L450-L480`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L450) |
| `GET /artist/?f=<id>&skip_tracks=true&offset=N&limit=N` | `f`, `skip_tracks`, `offset`, `limit` | Artist top tracks | [`js/api.js#L490-L540`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L490) |
| `GET /artist/bio/?id=<id>` | `id` | Artist biography | [`js/api.js#L542-L558`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L542) |
| `GET /artist/similar/?id=<id>` | `id` | Similar artists | [`js/api.js#L560-L580`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L560) |

### Mix

| Route | Query params | Description | Upstream citation |
|-------|-------------|-------------|-------------------|
| `GET /mix/?id=<id>` | `id` | Mix (editorial playlist) metadata + tracks | [`js/api.js#L505-L520`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L505) |

---

## Cover Art URL Pattern

```
https://resources.tidal.com/images/{uuid_as_path}/{size}x{size}.jpg
```

where `{uuid_as_path}` is the image UUID with `-` replaced by `/`.

**Sizes used:** 80, 160, 320, 640, 750, 1080, 1280 (px square)

**Source:** [`js/api.js#L683-L710`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L683)

**Examples:**
- Track / album cover (default 1280): `functions/track/[id].js#L68-L72`
- Artist picture (default 750): `functions/artist/[id].js#L53-L57`
- Playlist cover (default 1080): `functions/playlist/[id].js#L58-L62`

---

## Streaming Manifest Types

| Type | Detection | Handling | Upstream citation |
|------|-----------|----------|-------------------|
| **OriginalTrackUrl** | `OriginalTrackUrl` key present in response | Use directly as download URL | [`js/api.js#L557-L565`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L557) |
| **BTS JSON** | base64-decoded → `{"urls":[...]}` | Extract best URL by quality keyword rank | [`js/api.js#L307-L320`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L307) |
| **DASH MPD** | base64-decoded → contains `<MPD` | Use `DashDownloader` (browser) / chunked DASH fetch (server) | [`js/api.js#L302-L305`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L302) |
| **HLS** | URL contains `.m3u8` | Use `HlsDownloader` | [`js/api.js#L651-L657`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L651) |
| **Direct HTTPS** | URL starts with `https://` | Fetch with range requests | [`js/api.js#L659-L700`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L659) |

---

## MusicBrainz (artist socials)

The frontend optionally queries MusicBrainz for artist social links:

```
GET https://musicbrainz.org/ws/2/artist/?query=artist:<name>&fmt=json
GET https://musicbrainz.org/ws/2/artist/<mbid>?inc=url-rels&fmt=json
```

**Headers:** `User-Agent: Monochrome/2.0.0 ( https://github.com/monochrome-music/monochrome )`

**Source:** [`js/api.js#L44-L90`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/js/api.js#L44)

---

## Copyright Blocking

The following label/distributor name fragments cause content to be blocked
with a DMCA notice. They are encoded in base64 in the upstream source to
discourage trivial circumvention.

| Fragment | Decoded from |
|----------|-------------|
| `zee` | `emVl` |
| `zmc` | `em1j` |
| `zing music` | `emluZyBtdXNpYw==` |
| `etc bollywood` | `ZXRjIGJvbGx5d29vZA==` |
| `bollywood music` | `Ym9sbHl3b29kIG11c2lj` |
| `essel` | `ZXNzZWw=` |
| `zindagi` | `emluZGFnaQ==` |

**Source:** [`functions/track/[id].js#L127-L135`](https://github.com/monochrome-music/monochrome/blob/3347a2ea6d91eb472de8a7a6968301c957327fd8/functions/track/%5Bid%5D.js#L127)

---

## New Environment Variables (RubeTunes)

| Variable | Default | Description |
|----------|---------|-------------|
| `MONOCHROME_COUNTRY` | `US` | Country code passed to all Tidal API calls |
| `MONOCHROME_INSTANCES` | _(auto-discovered)_ | Comma-separated list of proxy instance base URLs; overrides auto-discovery |

---

*Credit: Backend logic ported from [monochrome-music/monochrome](https://github.com/monochrome-music/monochrome), which is itself a fork of [edideaur/monochrome](https://github.com/edideaur/monochrome).*
