"""rubetunes.spotify — Spotify backend ported from spotbye/SpotiFLAC.

Public re-exports for convenience:

    from rubetunes.spotify import SpotifyClient, TOTPGenerator, get_token

All Spotify operations use Spotify's internal GraphQL ``pathfinder`` endpoints
(api-partner.spotify.com/pathfinder/v1/query and /v2/query).
The public REST API (api.spotify.com/v1/*) is **never** called for track info,
album, playlist, search, or artist operations.

Credit: Logic ported from spotbye/SpotiFLAC (https://github.com/spotbye/SpotiFLAC).
"""
from __future__ import annotations

from rubetunes.spotify.totp import TOTPGenerator, generate_totp
from rubetunes.spotify.session import (
    get_session_client_version,
    get_anon_token,
    get_cc_token,
    get_client_token,
)
from rubetunes.spotify.client import SpotifyClient
from rubetunes.spotify_meta import get_token

__all__ = [
    "TOTPGenerator",
    "generate_totp",
    "get_session_client_version",
    "get_anon_token",
    "get_cc_token",
    "get_client_token",
    "SpotifyClient",
    "get_token",
]
