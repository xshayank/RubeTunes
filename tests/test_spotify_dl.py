# -*- coding: utf-8 -*-
"""
Unit tests for spotify_dl.py resolver functions.

All HTTP calls are mocked via the `responses` library so tests run fully
offline without any real network access or secrets.
"""
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import responses as resp_lib
import requests

# ---------------------------------------------------------------------------
# Make sure the repo root is on sys.path so we can import spotify_dl directly
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import spotify_dl as sdl  # noqa: E402  (import after path setup)


# ===========================================================================
# 1.  URL parsing — pure functions, no mocks needed
# ===========================================================================

class TestParseSpotifyTrackId:
    def test_full_url(self):
        assert sdl.parse_spotify_track_id(
            "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"
        ) == "4iV5W9uYEdYUVa79Axb7Rh"

    def test_uri(self):
        assert sdl.parse_spotify_track_id(
            "spotify:track:4iV5W9uYEdYUVa79Axb7Rh"
        ) == "4iV5W9uYEdYUVa79Axb7Rh"

    def test_bare_id(self):
        assert sdl.parse_spotify_track_id("4iV5W9uYEdYUVa79Axb7Rh") == "4iV5W9uYEdYUVa79Axb7Rh"

    def test_invalid(self):
        assert sdl.parse_spotify_track_id("https://example.com/not-spotify") is None

    def test_too_short(self):
        assert sdl.parse_spotify_track_id("short") is None


class TestParseTidalTrackId:
    def test_tidal_url(self):
        assert sdl.parse_tidal_track_id(
            "https://tidal.com/browse/track/123456789"
        ) == "123456789"

    def test_listen_tidal_url(self):
        assert sdl.parse_tidal_track_id(
            "https://listen.tidal.com/album/12345/track/98765"
        ) == "98765"

    def test_album_track_url(self):
        assert sdl.parse_tidal_track_id(
            "https://tidal.com/browse/album/SomeName/track/55555"
        ) == "55555"

    def test_invalid(self):
        assert sdl.parse_tidal_track_id("https://spotify.com/track/abc") is None


class TestParseQobuzTrackId:
    def test_open_qobuz_url(self):
        assert sdl.parse_qobuz_track_id(
            "https://open.qobuz.com/track/12345678"
        ) == "12345678"

    def test_qobuz_www_url(self):
        assert sdl.parse_qobuz_track_id(
            "https://www.qobuz.com/gb-en/album/some-album/12345/track/99999"
        ) == "99999"

    def test_bare_numeric_id(self):
        assert sdl.parse_qobuz_track_id("123456") == "123456"

    def test_invalid(self):
        assert sdl.parse_qobuz_track_id("notanid") is None


class TestParseAmazonTrackId:
    def test_tracks_url(self):
        assert sdl.parse_amazon_track_id(
            "https://music.amazon.com/tracks/B0ABCDE1234"
        ) == "B0ABCDE1234"

    def test_query_param(self):
        assert sdl.parse_amazon_track_id(
            "https://music.amazon.co.uk/albums/foo?trackAsin=B0ABCDE1234"
        ) == "B0ABCDE1234"

    def test_invalid(self):
        assert sdl.parse_amazon_track_id("https://example.com/not-amazon") is None


# ===========================================================================
# 2.  _resolve_deezer
# ===========================================================================

class TestResolveDeezer:
    ISRC = "USUM71703861"

    @resp_lib.activate
    def test_happy_path(self):
        resp_lib.add(
            resp_lib.GET,
            f"https://api.deezer.com/track/isrc:{self.ISRC}",
            json={"id": 1234567, "title": "Test Track", "artist": {"name": "Test Artist"}},
            status=200,
        )
        result = sdl._resolve_deezer(self.ISRC)
        assert result is not None
        assert result["id"] == 1234567

    @resp_lib.activate
    def test_no_match_404(self):
        resp_lib.add(
            resp_lib.GET,
            f"https://api.deezer.com/track/isrc:{self.ISRC}",
            json={"error": {"type": "DataNotFoundException", "message": "no data", "code": 800}},
            status=200,
        )
        assert sdl._resolve_deezer(self.ISRC) is None

    @resp_lib.activate
    def test_no_match_server_error(self):
        resp_lib.add(
            resp_lib.GET,
            f"https://api.deezer.com/track/isrc:{self.ISRC}",
            status=500,
        )
        assert sdl._resolve_deezer(self.ISRC) is None


# ===========================================================================
# 3.  _resolve_qobuz_by_isrc  (mocks the whole signed-request machinery)
# ===========================================================================

class TestResolveQobuzByIsrc:
    ISRC = "USUM71703861"

    @resp_lib.activate
    def test_happy_path(self):
        # Mock the scrape step and the API call.  We provide the signed URL
        # pattern as a passthrough to avoid coupling to the signing logic.
        resp_lib.add(
            resp_lib.GET,
            sdl._QOBUZ_API_BASE + "/track/search",
            json={
                "tracks": {
                    "items": [
                        {"id": 99, "title": "Test", "isrc": self.ISRC}
                    ],
                    "total": 1,
                }
            },
            status=200,
        )
        result = sdl._resolve_qobuz_by_isrc(self.ISRC)
        assert result is not None
        assert result["id"] == 99

    @resp_lib.activate
    def test_no_match(self):
        resp_lib.add(
            resp_lib.GET,
            sdl._QOBUZ_API_BASE + "/track/search",
            json={"tracks": {"items": [], "total": 0}},
            status=200,
        )
        assert sdl._resolve_qobuz_by_isrc(self.ISRC) is None


# ===========================================================================
# 4.  _resolve_tidal_by_isrc
# ===========================================================================

class TestResolveTidalByIsrc:
    ISRC = "USUM71703861"

    def test_no_token_returns_none(self):
        with patch.object(sdl, "TIDAL_TOKEN", ""):
            assert sdl._resolve_tidal_by_isrc(self.ISRC) is None

    @resp_lib.activate
    def test_happy_path_with_token(self):
        with patch.object(sdl, "TIDAL_TOKEN", "fake-token"):
            resp_lib.add(
                resp_lib.GET,
                sdl._TIDAL_API_BASE + "/tracks",
                json={"items": [{"id": 777, "title": "Tidal Track", "isrc": self.ISRC}]},
                status=200,
            )
            result = sdl._resolve_tidal_by_isrc(self.ISRC)
        assert result is not None
        assert result["id"] == 777

    @resp_lib.activate
    def test_empty_items(self):
        with patch.object(sdl, "TIDAL_TOKEN", "fake-token"):
            resp_lib.add(
                resp_lib.GET,
                sdl._TIDAL_API_BASE + "/tracks",
                json={"items": []},
                status=200,
            )
            assert sdl._resolve_tidal_by_isrc(self.ISRC) is None


# ===========================================================================
# 5.  _get_tidal_alt_url_by_tidal_id — four response shapes
# ===========================================================================

class TestGetTidalAltUrlByTidalId:
    TIDAL_ID = "12345678"

    def _single_base_patch(self):
        """Patch TIDAL_ALT_BASES to a single fake entry so tests are predictable."""
        return patch.object(sdl, "TIDAL_ALT_BASES", ["https://fake-tidal-alt.example.com/get"])

    @resp_lib.activate
    def test_redirect_shape(self):
        """301 redirect → return the Location URL."""
        with self._single_base_patch():
            resp_lib.add(
                resp_lib.GET,
                f"https://fake-tidal-alt.example.com/get/{self.TIDAL_ID}",
                status=301,
                headers={"Location": "https://cdn.example.com/track.flac"},
            )
            result = sdl._get_tidal_alt_url_by_tidal_id(self.TIDAL_ID)
        assert result == "https://cdn.example.com/track.flac"

    @resp_lib.activate
    def test_json_url_shape(self):
        """JSON body with a 'url' field → return the URL string."""
        with self._single_base_patch():
            resp_lib.add(
                resp_lib.GET,
                f"https://fake-tidal-alt.example.com/get/{self.TIDAL_ID}",
                json={"url": "https://cdn.example.com/track.flac"},
                status=200,
            )
            result = sdl._get_tidal_alt_url_by_tidal_id(self.TIDAL_ID)
        assert result == "https://cdn.example.com/track.flac"

    @resp_lib.activate
    def test_plain_text_url_shape(self):
        """text/plain response that is a URL → return it."""
        with self._single_base_patch():
            resp_lib.add(
                resp_lib.GET,
                f"https://fake-tidal-alt.example.com/get/{self.TIDAL_ID}",
                body="https://cdn.example.com/track.flac",
                content_type="text/plain",
                status=200,
            )
            result = sdl._get_tidal_alt_url_by_tidal_id(self.TIDAL_ID)
        assert result == "https://cdn.example.com/track.flac"

    @resp_lib.activate
    def test_v2_manifest_shape(self):
        """V2 BTS manifest → return a manifest dict."""
        manifest_inner = {
            "urls":     ["https://seg1.example.com/a.flac", "https://seg2.example.com/b.flac"],
            "codecs":   "flac",
            "mimeType": "audio/flac",
        }
        encoded = base64.b64encode(json.dumps(manifest_inner).encode()).decode()
        with self._single_base_patch():
            resp_lib.add(
                resp_lib.GET,
                f"https://fake-tidal-alt.example.com/get/{self.TIDAL_ID}",
                json={"data": {"manifest": encoded}},
                status=200,
            )
            result = sdl._get_tidal_alt_url_by_tidal_id(self.TIDAL_ID)
        assert isinstance(result, dict)
        assert result["type"] == "manifest"
        assert len(result["urls"]) == 2
        assert result["codecs"] == "flac"


# ===========================================================================
# 6.  _download_tidal_manifest — mock segment URLs, assert concatenated bytes
# ===========================================================================

class TestDownloadTidalManifest:
    @resp_lib.activate
    def test_concatenates_segments(self, tmp_path):
        seg1 = b"\x00\x01\x02\x03"
        seg2 = b"\x04\x05\x06\x07"
        resp_lib.add(resp_lib.GET, "https://seg1.example.com/a.flac", body=seg1, status=200)
        resp_lib.add(resp_lib.GET, "https://seg2.example.com/b.flac", body=seg2, status=200)

        manifest = {
            "type":     "manifest",
            "urls":     ["https://seg1.example.com/a.flac", "https://seg2.example.com/b.flac"],
            "codecs":   "flac",
            "mimeType": "audio/flac",
        }
        out_path = tmp_path / "out.flac"
        sdl._download_tidal_manifest(manifest, out_path)
        assert out_path.read_bytes() == seg1 + seg2

    def test_empty_urls_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="no URLs"):
            sdl._download_tidal_manifest({"urls": []}, tmp_path / "out.flac")


# ===========================================================================
# 7.  Circuit breaker state machine (monkey-patch time.time)
# ===========================================================================

class TestCircuitBreaker:
    SERVICE  = "test_svc"
    PROVIDER = "https://fake-provider.example.com/api"

    def _reset(self):
        """Remove any breaker state for the test key between sub-tests."""
        key = sdl._cb_key(self.SERVICE, self.PROVIDER)
        with sdl._circuit_lock:
            sdl._circuit_breakers.pop(key, None)

    def test_closed_by_default(self):
        self._reset()
        assert not sdl._is_circuit_open(self.SERVICE, self.PROVIDER)

    def test_opens_after_threshold_failures(self):
        self._reset()
        t0 = time.time()
        with patch("spotify_dl.time") as mock_time:
            mock_time.time.return_value = t0
            for _ in range(sdl.CIRCUIT_FAIL_THRESHOLD):
                sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, False)
            assert sdl._is_circuit_open(self.SERVICE, self.PROVIDER)

    def test_half_open_after_open_duration(self):
        self._reset()
        t0 = time.time()
        with patch("spotify_dl.time") as mock_time:
            mock_time.time.return_value = t0
            # Trip the breaker
            for _ in range(sdl.CIRCUIT_FAIL_THRESHOLD):
                sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, False)

            # Advance time past the open duration
            mock_time.time.return_value = t0 + sdl.CIRCUIT_OPEN_DURATION_SEC + 1
            # Now the circuit should transition to half-open (not open)
            assert not sdl._is_circuit_open(self.SERVICE, self.PROVIDER)
            key = sdl._cb_key(self.SERVICE, self.PROVIDER)
            with sdl._circuit_lock:
                state = sdl._circuit_breakers[key]["state"]
            assert state == sdl._CB_STATE_HALF_OPEN

    def test_closed_after_success_in_half_open(self):
        self._reset()
        t0 = time.time()
        with patch("spotify_dl.time") as mock_time:
            mock_time.time.return_value = t0
            for _ in range(sdl.CIRCUIT_FAIL_THRESHOLD):
                sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, False)

            # Advance to half-open
            mock_time.time.return_value = t0 + sdl.CIRCUIT_OPEN_DURATION_SEC + 1
            sdl._is_circuit_open(self.SERVICE, self.PROVIDER)  # triggers transition

            # Record a success → back to closed
            sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, True)
        key = sdl._cb_key(self.SERVICE, self.PROVIDER)
        with sdl._circuit_lock:
            state = sdl._circuit_breakers[key]["state"]
        assert state == sdl._CB_STATE_CLOSED

    def test_reopens_after_failure_in_half_open(self):
        self._reset()
        t0 = time.time()
        with patch("spotify_dl.time") as mock_time:
            mock_time.time.return_value = t0
            for _ in range(sdl.CIRCUIT_FAIL_THRESHOLD):
                sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, False)

            mock_time.time.return_value = t0 + sdl.CIRCUIT_OPEN_DURATION_SEC + 1
            sdl._is_circuit_open(self.SERVICE, self.PROVIDER)  # → half_open

            # Failure during half-open → re-open
            mock_time.time.return_value = t0 + sdl.CIRCUIT_OPEN_DURATION_SEC + 2
            sdl._record_provider_outcome(self.SERVICE, self.PROVIDER, False)
        key = sdl._cb_key(self.SERVICE, self.PROVIDER)
        with sdl._circuit_lock:
            state = sdl._circuit_breakers[key]["state"]
        assert state == sdl._CB_STATE_OPEN


# ===========================================================================
# 8.  LRU cache correctness and TTL expiry (monkey-patch time.time)
# ===========================================================================

class TestTrackInfoCache:
    def setup_method(self):
        """Start each test with an empty cache."""
        sdl.clear_track_info_cache()

    def test_set_and_get(self):
        sdl._cache_set_track_info("track_A", {"title": "A"})
        result = sdl._cache_get_track_info("track_A")
        assert result == {"title": "A"}

    def test_miss_returns_none(self):
        assert sdl._cache_get_track_info("nonexistent") is None

    def test_ttl_expiry(self):
        t0 = time.time()
        with patch("spotify_dl.time") as mock_time:
            mock_time.time.return_value = t0
            sdl._cache_set_track_info("track_B", {"title": "B"})

            # Still within TTL
            mock_time.time.return_value = t0 + sdl._TRACK_INFO_CACHE_TTL - 1
            assert sdl._cache_get_track_info("track_B") is not None

            # After TTL expires
            mock_time.time.return_value = t0 + sdl._TRACK_INFO_CACHE_TTL + 1
            assert sdl._cache_get_track_info("track_B") is None

    def test_lru_eviction(self):
        """When the cache is full, the least-recently-used entry is evicted."""
        max_entries = sdl._TRACK_INFO_CACHE_MAX
        for i in range(max_entries):
            sdl._cache_set_track_info(f"track_{i}", {"i": i})

        # Access track_0 to make it recently used
        sdl._cache_get_track_info("track_0")

        # Insert one more entry — should evict the LRU entry (track_1)
        sdl._cache_set_track_info("track_new", {"i": "new"})

        assert sdl._cache_get_track_info("track_new") is not None
        # track_0 was accessed recently so should survive; track_1 should be gone
        assert sdl._cache_get_track_info("track_0") is not None
        assert sdl._cache_get_track_info("track_1") is None

    def test_clear_returns_count(self):
        sdl._cache_set_track_info("x", {})
        sdl._cache_set_track_info("y", {})
        count = sdl.clear_track_info_cache()
        assert count == 2
        assert sdl._cache_get_track_info("x") is None


# ===========================================================================
# 9.  New Spotify URL parsers
# ===========================================================================

_BARE_22 = "4iV5W9uYEdYUVa79Axb7Rh"  # valid 22-char base62 id


class TestParseSpotifyPlaylistId:
    def test_full_url(self):
        assert sdl.parse_spotify_playlist_id(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        ) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_full_url_with_si_query(self):
        assert sdl.parse_spotify_playlist_id(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"
        ) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_spotify_uri(self):
        assert sdl.parse_spotify_playlist_id(
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
        ) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_invalid(self):
        assert sdl.parse_spotify_playlist_id("https://example.com/not-spotify") is None

    def test_bare_id_returns_none(self):
        assert sdl.parse_spotify_playlist_id(_BARE_22) is None


class TestParseSpotifyAlbumId:
    def test_full_url(self):
        assert sdl.parse_spotify_album_id(
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"
        ) == "1DFixLWuPkv3KT3TnV35m3"

    def test_full_url_with_si_query(self):
        assert sdl.parse_spotify_album_id(
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3?si=xyz"
        ) == "1DFixLWuPkv3KT3TnV35m3"

    def test_spotify_uri(self):
        assert sdl.parse_spotify_album_id(
            "spotify:album:1DFixLWuPkv3KT3TnV35m3"
        ) == "1DFixLWuPkv3KT3TnV35m3"

    def test_invalid(self):
        assert sdl.parse_spotify_album_id("https://example.com/not-spotify") is None

    def test_bare_id_returns_none(self):
        assert sdl.parse_spotify_album_id(_BARE_22) is None


class TestParseSpotifyArtistId:
    def test_full_url(self):
        assert sdl.parse_spotify_artist_id(
            "https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02"
        ) == "06HL4z0CvFAxyc27GXpf02"

    def test_full_url_with_si_query(self):
        assert sdl.parse_spotify_artist_id(
            "https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02?si=abc"
        ) == "06HL4z0CvFAxyc27GXpf02"

    def test_spotify_uri(self):
        assert sdl.parse_spotify_artist_id(
            "spotify:artist:06HL4z0CvFAxyc27GXpf02"
        ) == "06HL4z0CvFAxyc27GXpf02"

    def test_invalid(self):
        assert sdl.parse_spotify_artist_id("https://example.com/not-spotify") is None

    def test_bare_id_returns_none(self):
        assert sdl.parse_spotify_artist_id(_BARE_22) is None


# ===========================================================================
# 10.  New Spotify metadata fetchers (mocked via `responses`)
# ===========================================================================

_FAKE_TOKEN = "fake-access-token"
_PL_ID      = "37i9dQZF1DXcBWIGoYBM5M"
_ALB_ID     = "1DFixLWuPkv3KT3TnV35m3"
_ART_ID     = "06HL4z0CvFAxyc27GXpf02"


class TestGetSpotifyPlaylistTracks:
    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/playlists/{_PL_ID}",
                json={
                    "name": "Test Playlist",
                    "owner": {"display_name": "Test Owner"},
                    "images": [{"url": "https://example.com/img.jpg"}],
                    "tracks": {
                        "total": 2,
                        "next": None,
                        "items": [
                            {"track": {"id": "track1"}},
                            {"track": {"id": "track2"}},
                        ],
                    },
                },
                status=200,
            )
            info, track_ids = sdl.get_spotify_playlist_tracks(_PL_ID)

        assert info["name"] == "Test Playlist"
        assert info["owner"] == "Test Owner"
        assert info["total_tracks"] == 2
        assert info["image_url"] == "https://example.com/img.jpg"
        assert track_ids == ["track1", "track2"]

    @resp_lib.activate
    def test_skips_null_tracks(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/playlists/{_PL_ID}",
                json={
                    "name": "PL",
                    "owner": {"display_name": "o"},
                    "images": [],
                    "tracks": {
                        "total": 1,
                        "next": None,
                        "items": [
                            {"track": None},
                            {"track": {"id": "realtrack"}},
                        ],
                    },
                },
                status=200,
            )
            _, track_ids = sdl.get_spotify_playlist_tracks(_PL_ID)
        assert track_ids == ["realtrack"]


class TestGetSpotifyAlbumTracks:
    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/albums/{_ALB_ID}",
                json={
                    "name": "Test Album",
                    "artists": [{"name": "Artist A"}],
                    "release_date": "2023-01-01",
                    "total_tracks": 2,
                    "images": [{"url": "https://example.com/alb.jpg"}],
                    "tracks": {
                        "next": None,
                        "items": [
                            {"id": "t1"},
                            {"id": "t2"},
                        ],
                    },
                },
                status=200,
            )
            info, track_ids = sdl.get_spotify_album_tracks(_ALB_ID)

        assert info["name"] == "Test Album"
        assert info["artists"] == ["Artist A"]
        assert info["release_date"] == "2023-01-01"
        assert info["total_tracks"] == 2
        assert info["image_url"] == "https://example.com/alb.jpg"
        assert track_ids == ["t1", "t2"]


class TestGetSpotifyArtistInfo:
    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}",
                json={
                    "name": "Taylor Swift",
                    "images": [{"url": "https://example.com/ts.jpg"}],
                },
                status=200,
            )
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}/top-tracks",
                json={
                    "tracks": [
                        {
                            "id": "tid1",
                            "name": "Song A",
                            "artists": [{"name": "Taylor Swift"}],
                            "duration_ms": 225000,
                        }
                    ]
                },
                status=200,
            )
            info = sdl.get_spotify_artist_info(_ART_ID)

        assert info["name"] == "Taylor Swift"
        assert info["image_url"] == "https://example.com/ts.jpg"
        assert len(info["top_tracks"]) == 1
        t = info["top_tracks"][0]
        assert t["id"] == "tid1"
        assert t["title"] == "Song A"
        assert t["artists"] == ["Taylor Swift"]
        assert t["duration"] == "3:45"

    @resp_lib.activate
    def test_limits_top_tracks_to_5(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}",
                json={"name": "Artist", "images": []},
                status=200,
            )
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}/top-tracks",
                json={
                    "tracks": [
                        {"id": f"t{i}", "name": f"S{i}", "artists": [], "duration_ms": 0}
                        for i in range(10)
                    ]
                },
                status=200,
            )
            info = sdl.get_spotify_artist_info(_ART_ID)
        assert len(info["top_tracks"]) == 5


class TestGetSpotifyArtistAlbums:
    @resp_lib.activate
    def test_happy_path_albums(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}/albums",
                json={
                    "total": 3,
                    "items": [
                        {
                            "id": "alb1",
                            "name": "Album One",
                            "artists": [{"name": "Taylor Swift"}],
                            "release_date": "2020-01-01",
                            "total_tracks": 12,
                            "images": [{"url": "https://example.com/a1.jpg"}],
                        }
                    ],
                },
                status=200,
            )
            items, total = sdl.get_spotify_artist_albums(_ART_ID, "album", 0, 10)

        assert total == 3
        assert len(items) == 1
        a = items[0]
        assert a["id"] == "alb1"
        assert a["name"] == "Album One"
        assert a["artists"] == ["Taylor Swift"]
        assert a["release_date"] == "2020-01-01"
        assert a["total_tracks"] == 12
        assert a["image_url"] == "https://example.com/a1.jpg"

    @resp_lib.activate
    def test_happy_path_singles(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                f"https://api.spotify.com/v1/artists/{_ART_ID}/albums",
                json={"total": 0, "items": []},
                status=200,
            )
            items, total = sdl.get_spotify_artist_albums(_ART_ID, "single", 0, 10)
        assert total == 0
        assert items == []
