from __future__ import annotations

"""Constants ported from monochrome-music/monochrome.

Every constant below traces back to an upstream file + line range.
See docs/MONOCHROME_API_INVENTORY.md for the full endpoint inventory.
"""

# ---------------------------------------------------------------------------
# Tidal client credentials
# Source: functions/track/[id].js#L7-L8
#         functions/album/[id].js#L7-L8
#         functions/artist/[id].js#L7-L8
#         functions/playlist/[id].js#L7-L8
# ---------------------------------------------------------------------------
TIDAL_CLIENT_ID: str = "txNoH4kkV41MfH25"
TIDAL_CLIENT_SECRET: str = "dQjy0MinCEvxi1O4UmxvxWnDjt4cgHBPw8ll6nYBk98="

# ---------------------------------------------------------------------------
# Base URLs
# Source: functions/track/[id].js#L20 (auth), js/api.js (proxy routes)
# ---------------------------------------------------------------------------
TIDAL_AUTH_URL: str = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_API_BASE: str = "https://api.tidal.com/v1"
TIDAL_RESOURCES_BASE: str = "https://resources.tidal.com/images"

# ---------------------------------------------------------------------------
# Uptime / instance-discovery endpoint
# Source: functions/track/[id].js#L64 (INSTANCES_URLS)
# ---------------------------------------------------------------------------
TIDAL_UPTIME_URL: str = "https://tidal-uptime.geeked.wtf"

# ---------------------------------------------------------------------------
# Fallback proxy instances (used when uptime endpoint is unreachable)
# Source: functions/track/[id].js#L78-L91 (track fallbacks)
#         functions/album/[id].js#L78-L90 (album fallbacks, filters *.squid.wtf)
#         functions/playlist/[id].js#L77-L89 (playlist fallbacks)
#         js/api.js#L503-L523 (full LosslessAPI instance list)
# ---------------------------------------------------------------------------
DEFAULT_PROXY_INSTANCES: list[str] = [
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://arran.monochrome.tf",
    "https://triton.squid.wtf",
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://katze.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
    "https://wolf.qqdl.site",
]

# ---------------------------------------------------------------------------
# Default country code
# Source: functions/track/[id].js#L52, functions/album/[id].js#L42, etc.
# ---------------------------------------------------------------------------
DEFAULT_COUNTRY_CODE: str = "US"

# ---------------------------------------------------------------------------
# Audio quality tokens
# Source: js/api.js#L397-L409 (getTrackManifestFormats)
#         js/api.js#L415-L423 (getAudioQualityFromManifestFormats)
# ---------------------------------------------------------------------------
QUALITY_HI_RES_LOSSLESS: str = "HI_RES_LOSSLESS"
QUALITY_LOSSLESS: str = "LOSSLESS"
QUALITY_HIGH: str = "HIGH"
QUALITY_LOW: str = "LOW"
QUALITY_DOLBY_ATMOS: str = "DOLBY_ATMOS"

# ---------------------------------------------------------------------------
# Manifest format tokens (Tidal internal)
# Source: js/api.js#L397-L409
# ---------------------------------------------------------------------------
FORMAT_FLAC_HIRES: str = "FLAC_HIRES"
FORMAT_FLAC: str = "FLAC"
FORMAT_AACLC: str = "AACLC"
FORMAT_HEAACV1: str = "HEAACV1"
FORMAT_EAC3_JOC: str = "EAC3_JOC"  # Dolby Atmos

# ---------------------------------------------------------------------------
# Mapping: quality token → manifest format list
# Source: js/api.js#L397-L409 (getTrackManifestFormats)
# ---------------------------------------------------------------------------
QUALITY_TO_FORMATS: dict[str, list[str]] = {
    QUALITY_DOLBY_ATMOS:      [FORMAT_EAC3_JOC],
    QUALITY_HI_RES_LOSSLESS:  [FORMAT_FLAC_HIRES],
    QUALITY_LOSSLESS:         [FORMAT_FLAC],
    QUALITY_HIGH:             [FORMAT_AACLC],
    QUALITY_LOW:              [FORMAT_HEAACV1],
}

# ---------------------------------------------------------------------------
# Mapping: manifest format → quality token (inverse of above)
# Source: js/api.js#L415-L423 (getAudioQualityFromManifestFormats)
# ---------------------------------------------------------------------------
FORMAT_TO_QUALITY: dict[str, str] = {
    FORMAT_EAC3_JOC:   QUALITY_DOLBY_ATMOS,
    FORMAT_FLAC_HIRES: QUALITY_HI_RES_LOSSLESS,
    FORMAT_FLAC:       QUALITY_LOSSLESS,
    FORMAT_AACLC:      QUALITY_HIGH,
    FORMAT_HEAACV1:    QUALITY_LOW,
}

# ---------------------------------------------------------------------------
# Quality fallback chain for downloads
# Source: js/api.js (enrichTrack + downloadTrack fallback logic)
#         js/api.js#L603-L615 (DASH fallback to LOSSLESS if HI_RES fails)
# ---------------------------------------------------------------------------
QUALITY_FALLBACK_CHAIN: list[str] = [
    QUALITY_HI_RES_LOSSLESS,
    QUALITY_LOSSLESS,
    QUALITY_HIGH,
    QUALITY_LOW,
]

# ---------------------------------------------------------------------------
# Cover art sizes offered by resources.tidal.com
# Source: js/api.js#L683-L696 (getCoverSrcset)
#         functions/track/[id].js#L64 (getCoverUrl default 1280)
# ---------------------------------------------------------------------------
COVER_SIZES: list[int] = [80, 160, 320, 640, 750, 1080, 1280]
DEFAULT_COVER_SIZE: int = 1280
DEFAULT_ARTIST_PICTURE_SIZE: int = 750

# ---------------------------------------------------------------------------
# HTTP request defaults
# Source: js/api.js general fetchWithRetry behaviour
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT: float = 20.0
MAX_RETRIES_PER_INSTANCE: int = 2

# ---------------------------------------------------------------------------
# Token cache file name (under XDG_CACHE_HOME or ~/.cache)
# ---------------------------------------------------------------------------
TOKEN_CACHE_FILENAME: str = "monochrome_tidal_token.json"

# ---------------------------------------------------------------------------
# Copyright-blocked label fragments (base64-encoded in upstream source)
# Source: functions/track/[id].js#L127-L135
# ---------------------------------------------------------------------------
BLOCKED_COPYRIGHT_FRAGMENTS: list[str] = [
    "zee",
    "zmc",
    "zing music",
    "etc bollywood",
    "bollywood music",
    "essel",
    "zindagi",
]
