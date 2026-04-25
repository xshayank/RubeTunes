"""SpotifyClient — session-based web-player auth + pathfinder GraphQL.

Ported from spotbye/SpotiFLAC (``backend/spotfetch.go`` — SpotifyClient).

The client authenticates via the full web-player session flow (scrape
``open.spotify.com``, anonymous TOTP token, ``clienttoken.spotify.com``) and
then POSTs queries to the pathfinder/v2 GraphQL endpoint.

A module-level singleton (``_default_client``) is created lazily on first use
so that callers that only need the v1 persisted-query endpoint do not pay the
session-init cost.

Endpoint policy — this module NEVER calls api.spotify.com/v1/*.
All track/album/playlist/search/artist operations go through:
  - pathfinder/v1/query  (persisted GET queries via _spotify_graphql_query)
  - pathfinder/v2/query  (session POST queries via SpotifyClient.query)
"""
from __future__ import annotations

import json
import logging
import threading

import requests

from rubetunes.spotify.session import (
    get_session_client_version,
    get_anon_token,
    get_client_token,
)
from rubetunes.spotify_meta import (
    _spotify_graphql_query,
    _fetch_track_graphql,
    _fetch_album_graphql_page,
    _fetch_playlist_graphql_page,
    _fetch_artist_overview_graphql,
    _fetch_artist_discography_graphql,
    _fetch_search_graphql,
    _parse_graphql_track,
    _parse_graphql_artist,
    _parse_graphql_artist_discography,
    _parse_graphql_search,
    filter_track,
    filter_album,
    filter_playlist,
    get_spotify_playlist_tracks,
    get_spotify_album_tracks,
    get_spotify_artist_info,
    get_spotify_artist_albums,
    spotify_search,
)

log = logging.getLogger("spotify_dl")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


class SpotifyClient:
    """Session-based Spotify client using the web-player auth flow.

    Calls ``initialize()`` lazily on the first ``query()`` call.
    Wraps the high-level metadata helpers from ``rubetunes.spotify_meta`` so
    that callers have a single object to interact with.

    All operations use pathfinder GraphQL — ``api.spotify.com/v1/*`` is never
    called.
    """

    def __init__(self) -> None:
        self._session       = requests.Session()
        self._session.headers.update({"User-Agent": _UA})
        self._access_token  = ""
        self._client_token  = ""
        self._client_id     = ""
        self._device_id     = ""
        self._client_version = ""
        self._lock          = threading.Lock()

    # ------------------------------------------------------------------
    # Auth chain (ported from SpotiFLAC SpotifyClient.Initialize)
    # ------------------------------------------------------------------

    def _get_session_info(self) -> None:
        """Step 1: scrape open.spotify.com → clientVersion + sp_t cookie."""
        self._client_version = get_session_client_version(self._session)
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_access_token(self) -> None:
        """Step 2: TOTP anonymous token from open.spotify.com/api/token."""
        token, client_id = get_anon_token(self._session, self._client_version)
        self._access_token = token
        self._client_id    = client_id
        sp_t = self._session.cookies.get("sp_t")
        if sp_t:
            self._device_id = sp_t

    def _get_client_token(self) -> None:
        """Step 4: client-token from clienttoken.spotify.com."""
        if not self._client_id or not self._device_id or not self._client_version:
            self._get_session_info()
            self._get_access_token()
        self._client_token = get_client_token(
            self._session,
            self._client_id,
            self._device_id,
            self._client_version,
        )

    def initialize(self) -> None:
        """Run the full auth chain (steps 1–4)."""
        self._get_session_info()
        self._get_access_token()
        self._get_client_token()

    # ------------------------------------------------------------------
    # pathfinder/v2 GraphQL (session POST, requires client-token header)
    # ------------------------------------------------------------------

    def query(self, payload: dict) -> dict:
        """POST a GraphQL payload to pathfinder/v2/query.

        Lazily initializes the session on first call.
        Raises ``RuntimeError`` on non-200 responses.
        """
        with self._lock:
            if not self._access_token or not self._client_token:
                self.initialize()
        resp = self._session.post(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            json=payload,
            headers={
                "Authorization":       f"Bearer {self._access_token}",
                "Client-Token":        self._client_token,
                "Spotify-App-Version": self._client_version,
                "Content-Type":        "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Spotify pathfinder/v2 query failed: HTTP {resp.status_code}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # High-level helpers (delegate to spotify_meta GraphQL functions)
    # ------------------------------------------------------------------

    def get_track(self, track_id: str) -> dict:
        """Fetch track metadata via GraphQL ``getTrack``."""
        data = _fetch_track_graphql(track_id)
        return filter_track(data)

    def get_album(self, album_id: str, offset: int = 0, limit: int = 50) -> dict:
        """Fetch album metadata via GraphQL ``getAlbum``."""
        data = _fetch_album_graphql_page(album_id, offset, limit)
        return filter_album(data)

    def get_playlist(self, playlist_id: str, offset: int = 0, limit: int = 100) -> dict:
        """Fetch playlist metadata via GraphQL ``fetchPlaylist``."""
        data = _fetch_playlist_graphql_page(playlist_id, offset, limit)
        return filter_playlist(data)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search Spotify via GraphQL ``searchDesktop``."""
        return spotify_search(query, limit)

    def get_artist(self, artist_id: str) -> dict:
        """Fetch artist info via GraphQL ``queryArtistOverview``."""
        return get_spotify_artist_info(artist_id)

    def get_artist_top_tracks(self, artist_id: str) -> list[dict]:
        """Return the artist's top tracks (subset of get_artist)."""
        info = self.get_artist(artist_id)
        return info.get("top_tracks", [])

    def get_artist_albums(
        self,
        artist_id: str,
        group: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict], int]:
        """Return (items, total) for an artist's discography via GraphQL
        ``queryArtistDiscographyAll``.
        """
        return get_spotify_artist_albums(artist_id, group, offset, limit)


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_default_client: SpotifyClient | None = None
_default_client_lock = threading.Lock()


def get_default_client() -> SpotifyClient:
    """Return a lazily-created module-level SpotifyClient instance."""
    global _default_client
    if _default_client is None:
        with _default_client_lock:
            if _default_client is None:
                _default_client = SpotifyClient()
    return _default_client
