from __future__ import annotations

"""Async HTTP client for the monochrome/Tidal API.

Ported from monochrome-music/monochrome:
  - js/api.js   — LosslessAPI class (all proxy-facing routes)
  - functions/  — Direct Tidal API calls for track/album/artist/playlist
  - js/HiFi.ts  — Route response interfaces

The client has two layers that mirror the upstream architecture:
1. **Direct Tidal API** (api.tidal.com) — authenticated with the
   client-credentials token from ``auth.py``.
2. **Proxy instances** (monochrome.tf, qqdl.site, …) — a rotating pool of
   community-run HiFi API servers that proxy Tidal with richer endpoints.
   The client tries each instance in order and falls back to the direct API
   when all proxies are unavailable.

All public methods return typed model objects from ``models.py``.
"""

import asyncio
import logging
import os
import random
from typing import Any

import httpx

from rubetunes.providers.monochrome.auth import get_token
from rubetunes.providers.monochrome.constants import (
    DEFAULT_ARTIST_PICTURE_SIZE,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_COVER_SIZE,
    DEFAULT_PROXY_INSTANCES,
    MAX_RETRIES_PER_INSTANCE,
    QUALITY_LOSSLESS,
    REQUEST_TIMEOUT,
    TIDAL_API_BASE,
    TIDAL_RESOURCES_BASE,
    TIDAL_UPTIME_URL,
)
from rubetunes.providers.monochrome.manifest import (
    parse_playback_info,
    quality_to_formats,
)
from rubetunes.providers.monochrome.models import (
    Album,
    Artist,
    Playlist,
    SearchResult,
    StreamInfo,
    Track,
)

log = logging.getLogger(__name__)


class MonochromeClient:
    """Async Tidal/monochrome API client.

    Usage::

        async with MonochromeClient() as client:
            track = await client.get_track_metadata(12345678)
            stream = await client.get_stream_info(12345678, quality="LOSSLESS")
    """

    def __init__(
        self,
        country_code: str | None = None,
        proxy_instances: list[str] | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._country = country_code or os.environ.get(
            "MONOCHROME_COUNTRY", DEFAULT_COUNTRY_CODE
        )
        self._user_instances: list[str] = (
            [i.strip() for i in os.environ.get("MONOCHROME_INSTANCES", "").split(",") if i.strip()]
            if proxy_instances is None
            else proxy_instances
        )
        self._resolved_instances: list[str] | None = None
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._instance_lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Context manager
    # -----------------------------------------------------------------------

    async def __aenter__(self) -> "MonochromeClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("MonochromeClient must be used as an async context manager")
        return self._http

    # -----------------------------------------------------------------------
    # Instance discovery
    # Source: functions/track/[id].js — ServerAPI.getInstances()
    # -----------------------------------------------------------------------

    async def _get_instances(self, force_refresh: bool = False) -> list[str]:
        """Return the list of active proxy instances.

        Tries to fetch a live list from ``tidal-uptime.geeked.wtf``;
        falls back to the compile-time defaults on failure.

        Source: functions/track/[id].js#L64-L91 (ServerAPI.getInstances)
        """
        if self._user_instances:
            return self._user_instances

        async with self._instance_lock:
            if self._resolved_instances is not None and not force_refresh:
                return self._resolved_instances

            try:
                resp = await self._client.get(TIDAL_UPTIME_URL, timeout=5.0)
                if resp.is_success:
                    data = resp.json()
                    urls = [
                        item["url"] if isinstance(item, dict) else item
                        for item in (data.get("api") or [])
                    ]
                    if urls:
                        self._resolved_instances = urls
                        return urls
            except Exception as exc:
                log.debug("Could not fetch uptime instances: %s", exc)

            self._resolved_instances = list(DEFAULT_PROXY_INSTANCES)
            return self._resolved_instances

    # -----------------------------------------------------------------------
    # Proxy fetch with retry
    # Source: js/api.js — LosslessAPI.fetchWithRetry() (lines ~55-175)
    #         functions/track/[id].js — ServerAPI.fetchWithRetry()
    # -----------------------------------------------------------------------

    async def _proxy_get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET *path* from the first healthy proxy instance.

        Source: js/api.js — fetchWithRetry() (lines ~55-175)
        """
        instances = await self._get_instances()
        shuffled = list(instances)
        random.shuffle(shuffled)

        last_error: Exception | None = None
        attempts = 0
        for base in shuffled:
            url = (base.rstrip("/") + path) if path.startswith("/") else f"{base}/{path}"
            for _ in range(MAX_RETRIES_PER_INSTANCE):
                attempts += 1
                try:
                    resp = await self._client.get(url, **kwargs)
                    if resp.status_code == 429:
                        log.debug("Rate-limited by %s, trying next", base)
                        break
                    if resp.status_code >= 500:
                        log.debug("Server error %d from %s, trying next", resp.status_code, base)
                        break
                    resp.raise_for_status()
                    return resp
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(0.2)

        # All instances exhausted — try refreshing instance list once
        if not self._user_instances:
            instances = await self._get_instances(force_refresh=True)
            for base in instances[:3]:
                url = (base.rstrip("/") + path) if path.startswith("/") else f"{base}/{path}"
                try:
                    resp = await self._client.get(url, **kwargs)
                    resp.raise_for_status()
                    return resp
                except Exception as exc:
                    last_error = exc

        raise last_error or RuntimeError(f"All proxy instances failed for {path}")

    # -----------------------------------------------------------------------
    # Direct Tidal API helpers
    # Source: functions/track/[id].js — TidalAPI.fetchJson()
    # -----------------------------------------------------------------------

    async def _tidal_get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET *path* directly from api.tidal.com with bearer-token auth.

        Source: functions/track/[id].js — TidalAPI.fetchJson() (lines ~32-45)
        GET https://api.tidal.com/v1{path}
        Headers: Authorization: Bearer <token>
        Query: countryCode defaults to US
        """
        token = await get_token(self._client)
        merged: dict[str, str] = {"countryCode": self._country}
        if params:
            merged.update({k: str(v) for k, v in params.items()})
        resp = await self._client.get(
            f"{TIDAL_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=merged,
        )
        resp.raise_for_status()
        return resp

    # -----------------------------------------------------------------------
    # Cover / picture URL helpers
    # Source: js/api.js — getCoverUrl(), getArtistPictureUrl() (lines ~683-710)
    #         functions/track/[id].js — TidalAPI.getCoverUrl()
    # -----------------------------------------------------------------------

    def cover_url(self, cover_id: str | None, size: int = DEFAULT_COVER_SIZE) -> str:
        """Build a cover-art URL from a Tidal image UUID.

        The UUID uses ``-`` separators in the API but ``/`` in the CDN path.

        Source: js/api.js#L683-L696 (getCoverUrl)
        URL pattern: https://resources.tidal.com/images/{uuid/parts}/{size}x{size}.jpg
        """
        if not cover_id:
            return ""
        formatted = cover_id.replace("-", "/")
        return f"{TIDAL_RESOURCES_BASE}/{formatted}/{size}x{size}.jpg"

    def artist_picture_url(
        self, picture_id: str | None, size: int = DEFAULT_ARTIST_PICTURE_SIZE
    ) -> str:
        """Build an artist-picture URL from a Tidal image UUID.

        Source: js/api.js#L698-L710 (getArtistPictureUrl)
        """
        if not picture_id:
            return ""
        formatted = picture_id.replace("-", "/")
        return f"{TIDAL_RESOURCES_BASE}/{formatted}/{size}x{size}.jpg"

    # -----------------------------------------------------------------------
    # Search
    # Source: js/api.js — search(), searchTracks(), searchAlbums(), etc.
    #         Routes: GET /search/?q=..., /search/?s=..., etc.
    # -----------------------------------------------------------------------

    async def search(self, query: str) -> SearchResult:
        """Combined search (tracks + albums + artists + playlists).

        Source: js/api.js — search() (lines ~205-255)
        Proxy route: GET /search/?q=<query>
        """
        resp = await self._proxy_get(f"/search/?q={_enc(query)}")
        data = resp.json()
        return _parse_search_result(data)

    async def search_tracks(self, query: str) -> list[Track]:
        """Track-only search.

        Source: js/api.js — searchTracks() (lines ~257-280)
        Proxy route: GET /search/?s=<query>
        """
        resp = await self._proxy_get(f"/search/?s={_enc(query)}")
        data = resp.json()
        return _extract_items(data, "tracks", Track.from_dict)

    async def search_artists(self, query: str) -> list[Artist]:
        """Artist search.

        Source: js/api.js — searchArtists() (lines ~282-302)
        Proxy route: GET /search/?a=<query>
        """
        resp = await self._proxy_get(f"/search/?a={_enc(query)}")
        data = resp.json()
        return _extract_items(data, "artists", Artist.from_dict)

    async def search_albums(self, query: str) -> list[Album]:
        """Album search.

        Source: js/api.js — searchAlbums() (lines ~304-325)
        Proxy route: GET /search/?al=<query>
        """
        resp = await self._proxy_get(f"/search/?al={_enc(query)}")
        data = resp.json()
        return _extract_items(data, "albums", Album.from_dict)

    async def search_playlists(self, query: str) -> list[Playlist]:
        """Playlist search.

        Source: js/api.js — searchPlaylists() (lines ~327-345)
        Proxy route: GET /search/?p=<query>
        """
        resp = await self._proxy_get(f"/search/?p={_enc(query)}")
        data = resp.json()
        return _extract_items(data, "playlists", Playlist.from_dict)

    # -----------------------------------------------------------------------
    # Track metadata
    # Source: js/api.js — getTrackMetadata() (lines ~500-520)
    #         Proxy route: GET /info/?id=<id>
    #         Direct Tidal: GET https://api.tidal.com/v1/tracks/{id}/?countryCode=US
    # -----------------------------------------------------------------------

    async def get_track_metadata(self, track_id: int | str) -> Track:
        """Fetch track metadata.

        Tries the proxy first; falls back to the direct Tidal API.

        Source: js/api.js — getTrackMetadata() (lines ~500-520)
        Proxy route:  GET /info/?id=<track_id>
        Direct Tidal: GET https://api.tidal.com/v1/tracks/{id}/ ?countryCode=US
                      Headers: Authorization: Bearer <token>
        """
        try:
            resp = await self._proxy_get(f"/info/?id={track_id}")
            data = resp.json()
            raw = data.get("data") or data
            items = raw if isinstance(raw, list) else [raw]
            found = next(
                (i for i in items if str(i.get("id", "")) == str(track_id)
                 or (i.get("item") and str(i["item"].get("id", "")) == str(track_id))),
                None,
            )
            if found:
                return Track.from_dict(found.get("item") or found)
        except Exception as exc:
            log.debug("Proxy track metadata failed, trying direct Tidal: %s", exc)

        resp = await self._tidal_get(f"/tracks/{track_id}/")
        return Track.from_dict(resp.json())

    # -----------------------------------------------------------------------
    # Album metadata + tracks
    # Source: js/api.js — getAlbum() (lines ~340-430)
    #         Proxy route: GET /album/?id=<id>[&offset=N&limit=N]
    #         Direct Tidal: GET https://api.tidal.com/v1/albums/{id}?countryCode=US
    # -----------------------------------------------------------------------

    async def get_album(
        self, album_id: int | str, *, include_tracks: bool = True
    ) -> tuple[Album, list[Track]]:
        """Fetch album metadata and (optionally) its full track list.

        Returns ``(album, tracks)`` tuple.  Handles pagination automatically.

        Source: js/api.js — getAlbum() (lines ~340-430)
        Proxy route:  GET /album/?id=<album_id>
                      GET /album/?id=<album_id>&offset=<N>&limit=500  (pagination)
        Direct Tidal: GET https://api.tidal.com/v1/albums/{id}?countryCode=US
        """
        try:
            resp = await self._proxy_get(f"/album/?id={album_id}")
            data = (resp.json().get("data") or resp.json())

            album_raw = None
            tracks_raw: list[dict] = []

            if isinstance(data, dict):
                if "numberOfTracks" in data or "title" in data:
                    album_raw = data
                if "items" in data:
                    tracks_raw = data["items"]
            elif isinstance(data, list):
                for entry in data:
                    if not album_raw and ("numberOfTracks" in entry or "title" in entry):
                        album_raw = entry
                    if not tracks_raw and "items" in entry:
                        tracks_raw = entry["items"]

            if album_raw is None:
                raise ValueError("album not found in proxy response")

            album = Album.from_dict(album_raw)
            tracks = [Track.from_dict(i.get("item") or i) for i in tracks_raw]

            # Paginate if more tracks exist
            if include_tracks and album.number_of_tracks > len(tracks):
                offset = len(tracks)
                while len(tracks) < album.number_of_tracks:
                    try:
                        pr = await self._proxy_get(
                            f"/album/?id={album_id}&offset={offset}&limit=500"
                        )
                        page = pr.json().get("data") or pr.json()
                        page_items: list[dict] = []
                        if isinstance(page, dict) and "items" in page:
                            page_items = page["items"]
                        elif isinstance(page, list):
                            for e in page:
                                if isinstance(e, dict) and "items" in e:
                                    page_items = e["items"]
                                    break
                        if not page_items:
                            break
                        new = [Track.from_dict(i.get("item") or i) for i in page_items]
                        if new and tracks and new[0].id == tracks[0].id:
                            break  # API ignoring offset — bail out
                        tracks.extend(new)
                        offset += len(new)
                    except Exception:
                        break

            return album, tracks

        except Exception as exc:
            log.debug("Proxy album fetch failed, trying direct Tidal: %s", exc)

        resp = await self._tidal_get(f"/albums/{album_id}")
        album = Album.from_dict(resp.json())
        return album, []

    # -----------------------------------------------------------------------
    # Playlist metadata + tracks
    # Source: js/api.js — getPlaylist() (lines ~432-498)
    #         Proxy route: GET /playlist/?id=<id>[&offset=N]
    #         Direct Tidal: GET https://api.tidal.com/v1/playlists/{id}?countryCode=US
    # -----------------------------------------------------------------------

    async def get_playlist(self, playlist_id: str) -> tuple[Playlist, list[Track]]:
        """Fetch playlist metadata and its full track list.

        Returns ``(playlist, tracks)`` tuple.

        Source: js/api.js — getPlaylist() (lines ~432-498)
        Proxy route:  GET /playlist/?id=<playlist_id>
        Direct Tidal: GET https://api.tidal.com/v1/playlists/{id}?countryCode=US
        """
        try:
            resp = await self._proxy_get(f"/playlist/?id={playlist_id}")
            data = resp.json().get("data") or resp.json()

            playlist_raw = None
            tracks_raw: list[dict] = []

            if isinstance(data, dict):
                playlist_raw = data.get("playlist") or (
                    data if ("uuid" in data or "numberOfTracks" in data) else None
                )
                if "items" in data:
                    tracks_raw = data["items"]
            elif isinstance(data, list):
                for entry in data:
                    if not playlist_raw and ("uuid" in entry or "numberOfTracks" in entry):
                        playlist_raw = entry
                    if not tracks_raw and "items" in entry:
                        tracks_raw = entry["items"]

            if playlist_raw is None:
                raise ValueError("playlist not found in proxy response")

            playlist = Playlist.from_dict(playlist_raw)
            tracks = [Track.from_dict(i.get("item") or i) for i in tracks_raw]

            # Paginate
            if playlist.number_of_tracks > len(tracks):
                offset = len(tracks)
                while len(tracks) < playlist.number_of_tracks:
                    try:
                        pr = await self._proxy_get(
                            f"/playlist/?id={playlist_id}&offset={offset}"
                        )
                        page = pr.json().get("data") or pr.json()
                        page_items: list[dict] = []
                        if isinstance(page, dict) and "items" in page:
                            page_items = page["items"]
                        elif isinstance(page, list):
                            for e in page:
                                if isinstance(e, dict) and "items" in e:
                                    page_items = e["items"]
                                    break
                        if not page_items:
                            break
                        new = [Track.from_dict(i.get("item") or i) for i in page_items]
                        if new and tracks and new[0].id == tracks[0].id:
                            break
                        tracks.extend(new)
                        offset += len(new)
                    except Exception:
                        break

            return playlist, tracks

        except Exception as exc:
            log.debug("Proxy playlist fetch failed, trying direct Tidal: %s", exc)

        resp = await self._tidal_get(f"/playlists/{playlist_id}")
        playlist = Playlist.from_dict(resp.json())
        return playlist, []

    # -----------------------------------------------------------------------
    # Artist metadata
    # Source: js/api.js — getArtist() (lines ~450-480)
    #         Proxy route: GET /artist/?id=<id>
    #         Direct Tidal: GET https://api.tidal.com/v1/artists/{id}?countryCode=US
    # -----------------------------------------------------------------------

    async def get_artist(self, artist_id: int | str) -> Artist:
        """Fetch artist profile.

        Source: js/api.js — getArtist() (lines ~450-480)
        Proxy route:  GET /artist/?id=<artist_id>
        Direct Tidal: GET https://api.tidal.com/v1/artists/{id}?countryCode=US
        """
        try:
            resp = await self._proxy_get(f"/artist/?id={artist_id}")
            data = resp.json().get("data") or resp.json()
            raw_artist = (
                data.get("artist")
                or (data[0] if isinstance(data, list) and data else data)
            )
            if raw_artist:
                return Artist.from_dict(raw_artist)
        except Exception as exc:
            log.debug("Proxy artist fetch failed, trying direct Tidal: %s", exc)

        resp = await self._tidal_get(f"/artists/{artist_id}")
        return Artist.from_dict(resp.json())

    async def get_artist_top_tracks(
        self, artist_id: int | str, limit: int = 15, offset: int = 0
    ) -> list[Track]:
        """Fetch artist's top tracks.

        Source: js/api.js — getArtistTopTracks() (lines ~490-540)
        Proxy route: GET /artist/?f=<artist_id>&skip_tracks=true&offset=N&limit=N
        """
        try:
            resp = await self._proxy_get(
                f"/artist/?f={artist_id}&skip_tracks=true&offset={offset}&limit={limit}"
            )
            data = resp.json().get("data") or resp.json()
            raw_tracks = data.get("tracks") or []
            return [Track.from_dict(t) for t in raw_tracks]
        except Exception as exc:
            log.debug("Artist top-tracks fetch failed: %s", exc)
            return []

    async def get_artist_biography(self, artist_id: int | str) -> dict | None:
        """Fetch artist biography text.

        Source: js/api.js — getArtistBiography() (lines ~542-558)
        Proxy route: GET /artist/bio/?id=<artist_id>
        """
        try:
            resp = await self._proxy_get(f"/artist/bio/?id={artist_id}")
            body = resp.json()
            bio_data = body.get("data") or body
            if bio_data and bio_data.get("text"):
                return {"text": bio_data["text"], "source": bio_data.get("source", "Tidal")}
        except Exception as exc:
            log.debug("Artist biography fetch failed: %s", exc)
        return None

    async def get_similar_artists(self, artist_id: int | str) -> list[Artist]:
        """Fetch similar artists.

        Source: js/api.js — getSimilarArtists() (lines ~560-580)
        Proxy route: GET /artist/similar/?id=<artist_id>
        """
        try:
            resp = await self._proxy_get(f"/artist/similar/?id={artist_id}")
            data = resp.json()
            items = data.get("artists") or data.get("items") or data.get("data") or []
            return [Artist.from_dict(a) for a in (items if isinstance(items, list) else [])]
        except Exception as exc:
            log.debug("Similar artists fetch failed: %s", exc)
            return []

    async def get_similar_albums(self, album_id: int | str) -> list[Album]:
        """Fetch similar albums.

        Source: js/api.js — getSimilarAlbums() (lines ~582-600)
        Proxy route: GET /album/similar/?id=<album_id>
        """
        try:
            resp = await self._proxy_get(f"/album/similar/?id={album_id}")
            data = resp.json()
            items = data.get("items") or data.get("albums") or data.get("data") or []
            return [Album.from_dict(a) for a in (items if isinstance(items, list) else [])]
        except Exception as exc:
            log.debug("Similar albums fetch failed: %s", exc)
            return []

    # -----------------------------------------------------------------------
    # Track stream / manifest
    # Source: js/api.js — getTrack() (lines ~530-555)
    #         Proxy route: GET /trackManifests/?id=N&quality=Q&formats=F&adaptive=false
    #
    #         Also covers:
    #         js/api.js — getStreamUrl() (lines ~557-590)
    #         Proxy route: GET /stream?id=N&quality=Q
    # -----------------------------------------------------------------------

    async def get_stream_info(
        self, track_id: int | str, quality: str = QUALITY_LOSSLESS
    ) -> StreamInfo:
        """Resolve a track's streaming manifest and return a StreamInfo object.

        Tries ``/trackManifests/`` first, then falls back to ``/stream``.

        Source: js/api.js — getTrack() (lines ~530-555)
        Proxy route: GET /trackManifests/?id=<id>&quality=<Q>&formats=<F>
        Fallback:    GET /stream?id=<id>&quality=<Q>
        """
        formats = quality_to_formats(quality)
        params_str = f"id={track_id}&quality={quality}&adaptive=false"
        for fmt in formats:
            params_str += f"&formats={fmt}"

        try:
            resp = await self._proxy_get(f"/trackManifests/?{params_str}")
            raw = resp.json()
            info_dict = parse_playback_info(raw)
            return StreamInfo.from_dict(info_dict)
        except Exception as exc:
            log.debug("trackManifests failed, trying /stream: %s", exc)

        try:
            resp = await self._proxy_get(f"/stream?id={track_id}&quality={quality}")
            data = resp.json()
            url = data.get("url") or data.get("streamUrl")
            if url:
                return StreamInfo(
                    track_id=int(track_id),
                    audio_quality=quality,
                    original_track_url=url,
                )
        except Exception as exc:
            log.debug("/stream also failed: %s", exc)

        # Last resort: direct Tidal playbackinfo
        # Source: functions/track/[id].js#L53-L62 (TidalAPI.getStreamUrl)
        # GET https://api.tidal.com/v1/tracks/{id}/playbackinfo
        #     ?audioquality=LOW&playbackmode=STREAM&assetpresentation=FULL&countryCode=US
        resp = await self._tidal_get(
            f"/tracks/{track_id}/playbackinfo",
            params={
                "audioquality": "LOW",
                "playbackmode": "STREAM",
                "assetpresentation": "FULL",
            },
        )
        data = resp.json()
        url = data.get("url") or data.get("streamUrl")
        return StreamInfo(
            track_id=int(track_id),
            audio_quality=data.get("audioQuality", quality),
            manifest=data.get("manifest", ""),
            original_track_url=url,
        )

    async def get_track_recommendations(self, track_id: int | str) -> list[Track]:
        """Fetch recommended tracks.

        Source: js/api.js — getTrackRecommendations() (lines ~522-530)
        Proxy route: GET /recommendations/?id=<track_id>
        """
        try:
            resp = await self._proxy_get(f"/recommendations/?id={track_id}")
            data = resp.json().get("data") or resp.json()
            items = data.get("items") or []
            return [Track.from_dict(i.get("track") or i) for i in items]
        except Exception as exc:
            log.debug("Recommendations fetch failed: %s", exc)
            return []

    # -----------------------------------------------------------------------
    # Mix
    # Source: js/api.js — getMix() (lines ~505-520)
    #         Proxy route: GET /mix/?id=<id>
    # -----------------------------------------------------------------------

    async def get_mix(self, mix_id: str) -> tuple[dict, list[Track]]:
        """Fetch a Tidal Mix (editorial playlist).

        Returns ``(mix_metadata_dict, tracks)`` tuple.

        Source: js/api.js — getMix() (lines ~505-520)
        Proxy route: GET /mix/?id=<mix_id>
        """
        resp = await self._proxy_get(f"/mix/?id={mix_id}")
        data = resp.json()
        mix_data = data.get("mix") or {}
        items = data.get("items") or []
        tracks = [Track.from_dict(i.get("item") or i) for i in items]
        return mix_data, tracks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _enc(s: str) -> str:
    """URL-encode a query string value."""
    from urllib.parse import quote
    return quote(s, safe="")


def _find_section(
    source: Any, key: str, visited: set[int] | None = None
) -> dict | None:
    """Recursively find the first object that contains an ``items`` list.

    Mirrors LosslessAPI.findSearchSection() in js/api.js (lines ~180-200).
    """
    if visited is None:
        visited = set()

    if source is None or not isinstance(source, (dict, list)):
        return None

    obj_id = id(source)
    if obj_id in visited:
        return None
    visited.add(obj_id)

    if isinstance(source, list):
        for elem in source:
            result = _find_section(elem, key, visited)
            if result is not None:
                return result
        return None

    if "items" in source and isinstance(source["items"], list):
        return source

    if key in source:
        result = _find_section(source[key], key, visited)
        if result is not None:
            return result

    for v in source.values():
        result = _find_section(v, key, visited)
        if result is not None:
            return result

    return None


def _extract_items(
    data: Any, key: str, factory: Any
) -> list:
    """Extract a typed list from a nested search/response payload."""
    section = _find_section(data, key)
    if section is None:
        return []
    return [factory(item) for item in (section.get("items") or [])]


def _parse_search_result(data: Any) -> SearchResult:
    """Build a SearchResult from a combined-search response.

    Source: js/api.js — search() return value (lines ~225-255)
    """
    tracks = _extract_items(data, "tracks", Track.from_dict)
    albums = _extract_items(data, "albums", Album.from_dict)
    artists = _extract_items(data, "artists", Artist.from_dict)
    playlists = _extract_items(data, "playlists", Playlist.from_dict)

    _count_by_key = {
        "tracks": len(tracks),
        "albums": len(albums),
        "artists": len(artists),
        "playlists": len(playlists),
    }

    def _total(d: Any, key: str) -> int:
        section = _find_section(d, key)
        return int((section or {}).get("totalNumberOfItems", _count_by_key.get(key, 0)))

    return SearchResult(
        tracks=tracks,
        albums=albums,
        artists=artists,
        playlists=playlists,
        total_tracks=_total(data, "tracks"),
        total_albums=_total(data, "albums"),
        total_artists=_total(data, "artists"),
        total_playlists=_total(data, "playlists"),
    )
