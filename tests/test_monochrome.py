# -*- coding: utf-8 -*-
"""
Tests for the monochrome/Tidal provider.

Covers:
1. Auth bootstrap — asserts the exact upstream URL, method, headers, and body.
2. Manifest parser — BTS JSON, DASH MPD, direct URL, and object forms.
3. Quality-selection — HI_RES_LOSSLESS falls back to LOSSLESS then HIGH then LOW.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(payload: dict | str) -> str:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return base64.b64encode(payload.encode()).decode()


# ===========================================================================
# 1. Auth bootstrap
# ===========================================================================

class TestAuthBootstrap:
    """Verify that get_token() hits exactly the right endpoint."""

    def setup_method(self) -> None:
        """Clear the in-process token cache before each test."""
        from rubetunes.providers.monochrome.auth import clear_token_cache
        clear_token_cache()

    @pytest.mark.asyncio
    async def test_token_request_url_method_headers_body(self) -> None:
        """get_token must POST to https://auth.tidal.com/v1/oauth2/token with
        the correct Authorization header and grant_type body.

        Source: functions/track/[id].js#L11-L30 (TidalAPI.getToken)
        """
        from rubetunes.providers.monochrome.auth import _fetch_new_token
        from rubetunes.providers.monochrome.constants import (
            TIDAL_AUTH_URL,
            TIDAL_CLIENT_ID,
            TIDAL_CLIENT_SECRET,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "access_token": "test_token_abc",
            "expires_in": 3600,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        token, expires_in = await _fetch_new_token(mock_client)

        # Assert the call was made
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args

        # URL must be exact
        assert call_kwargs.args[0] == TIDAL_AUTH_URL, (
            f"Expected {TIDAL_AUTH_URL!r}, got {call_kwargs.args[0]!r}"
        )

        # Authorization header must be Basic base64(client_id:client_secret)
        expected_b64 = base64.b64encode(
            f"{TIDAL_CLIENT_ID}:{TIDAL_CLIENT_SECRET}".encode()
        ).decode()
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Authorization") == f"Basic {expected_b64}"
        assert headers.get("Content-Type") == "application/x-www-form-urlencoded"

        # Body must include grant_type=client_credentials
        body = call_kwargs.kwargs.get("data", {})
        assert body.get("grant_type") == "client_credentials"
        assert body.get("client_id") == TIDAL_CLIENT_ID
        assert body.get("client_secret") == TIDAL_CLIENT_SECRET

        # Return values
        assert token == "test_token_abc"
        assert expires_in == 3600

    @pytest.mark.asyncio
    async def test_get_token_caches_result(self) -> None:
        """Calling get_token twice should only hit the network once."""
        from rubetunes.providers.monochrome.auth import clear_token_cache, get_token
        clear_token_cache()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"access_token": "cached_tok", "expires_in": 3600}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        t1 = await get_token(mock_client)
        t2 = await get_token(mock_client)

        assert t1 == t2 == "cached_tok"
        assert mock_client.post.call_count == 1, "Token should be cached after first fetch"


# ===========================================================================
# 2. Manifest parser
# ===========================================================================

class TestManifestParser:
    """Verify that extract_stream_url handles all manifest types correctly."""

    def test_none_returns_none(self) -> None:
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        assert extract_stream_url(None) is None

    def test_plain_https_url_passthrough(self) -> None:
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        url = "https://cf.tidal.com/audio/12345.flac?token=abc"
        assert extract_stream_url(url) == url

    def test_bts_json_single_url(self) -> None:
        """Base64-encoded JSON with a single ``urls`` entry."""
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        payload = {"urls": ["https://example.com/track.flac"]}
        manifest = _b64(payload)
        assert extract_stream_url(manifest) == "https://example.com/track.flac"

    def test_bts_json_multiple_urls_picks_best(self) -> None:
        """When multiple URLs are present, the one with 'flac' in it wins."""
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        payload = {
            "urls": [
                "https://example.com/track.aac",
                "https://example.com/track.flac?hi-res=1",
                "https://example.com/track.mp4",
            ]
        }
        manifest = _b64(payload)
        result = extract_stream_url(manifest)
        # 'flac' keyword is highest priority
        assert "flac" in result

    def test_raw_dict_with_urls(self) -> None:
        """Already-decoded dict with urls list."""
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        manifest = {"urls": ["https://cdn.tidal.com/audio.flac"]}
        assert extract_stream_url(manifest) == "https://cdn.tidal.com/audio.flac"

    def test_dash_manifest_no_base_url_returns_none(self) -> None:
        """DASH MPD without BaseURL returns None (caller must use DASH downloader)."""
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        mpd_xml = '<?xml version="1.0"?><MPD type="static"><Period></Period></MPD>'
        manifest = _b64({"_": mpd_xml})  # encode as JSON to force base64 path
        # Encode the raw XML directly
        manifest2 = base64.b64encode(mpd_xml.encode()).decode()
        result = extract_stream_url(manifest2)
        assert result is None

    def test_dash_manifest_with_base_url(self) -> None:
        """DASH MPD with BaseURL extracts the first segment URL."""
        from rubetunes.providers.monochrome.manifest import extract_stream_url
        mpd_xml = (
            '<?xml version="1.0"?>'
            '<MPD type="static">'
            '<Period><AdaptationSet>'
            '<Representation><BaseURL>https://cdn.tidal.com/seg.flac</BaseURL>'
            '</Representation></AdaptationSet></Period>'
            '</MPD>'
        )
        manifest = base64.b64encode(mpd_xml.encode()).decode()
        result = extract_stream_url(manifest)
        assert result == "https://cdn.tidal.com/seg.flac"

    def test_is_dash_manifest_true(self) -> None:
        from rubetunes.providers.monochrome.manifest import is_dash_manifest
        mpd = base64.b64encode(b"<MPD type='static'></MPD>").decode()
        assert is_dash_manifest(mpd) is True

    def test_is_dash_manifest_false(self) -> None:
        from rubetunes.providers.monochrome.manifest import is_dash_manifest
        bts = _b64({"urls": ["https://example.com/a.flac"]})
        assert is_dash_manifest(bts) is False


# ===========================================================================
# 3. Quality selection / fallback chain
# ===========================================================================

class TestQualitySelection:
    """Verify the quality fallback chain logic.

    Source: js/api.js — enrichTrack() + downloadTrack() fallback (lines ~603-615)
    """

    def test_hi_res_available_returns_hi_res(self) -> None:
        from rubetunes.providers.monochrome.manifest import select_quality
        result = select_quality(
            "HI_RES_LOSSLESS",
            available=["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
        )
        assert result == "HI_RES_LOSSLESS"

    def test_hi_res_missing_falls_back_to_lossless(self) -> None:
        from rubetunes.providers.monochrome.manifest import select_quality
        result = select_quality(
            "HI_RES_LOSSLESS",
            available=["LOSSLESS", "HIGH", "LOW"],
        )
        assert result == "LOSSLESS"

    def test_hi_res_and_lossless_missing_falls_back_to_high(self) -> None:
        from rubetunes.providers.monochrome.manifest import select_quality
        result = select_quality(
            "HI_RES_LOSSLESS",
            available=["HIGH", "LOW"],
        )
        assert result == "HIGH"

    def test_only_low_available(self) -> None:
        from rubetunes.providers.monochrome.manifest import select_quality
        result = select_quality(
            "HI_RES_LOSSLESS",
            available=["LOW"],
        )
        assert result == "LOW"

    def test_no_available_returns_requested(self) -> None:
        """If available is None, return the requested quality as-is."""
        from rubetunes.providers.monochrome.manifest import select_quality
        result = select_quality("HI_RES_LOSSLESS", available=None)
        assert result == "HI_RES_LOSSLESS"

    def test_quality_to_formats_hi_res(self) -> None:
        from rubetunes.providers.monochrome.manifest import quality_to_formats
        assert quality_to_formats("HI_RES_LOSSLESS") == ["FLAC_HIRES"]

    def test_quality_to_formats_lossless(self) -> None:
        from rubetunes.providers.monochrome.manifest import quality_to_formats
        assert quality_to_formats("LOSSLESS") == ["FLAC"]

    def test_quality_to_formats_high(self) -> None:
        from rubetunes.providers.monochrome.manifest import quality_to_formats
        assert quality_to_formats("HIGH") == ["AACLC"]

    def test_quality_to_formats_low(self) -> None:
        from rubetunes.providers.monochrome.manifest import quality_to_formats
        assert quality_to_formats("LOW") == ["HEAACV1"]

    def test_quality_to_formats_dolby_atmos(self) -> None:
        from rubetunes.providers.monochrome.manifest import quality_to_formats
        assert quality_to_formats("DOLBY_ATMOS") == ["EAC3_JOC"]

    def test_formats_to_quality_flac_hires(self) -> None:
        from rubetunes.providers.monochrome.manifest import formats_to_quality
        assert formats_to_quality(["FLAC_HIRES"]) == "HI_RES_LOSSLESS"

    def test_formats_to_quality_priority(self) -> None:
        """When multiple formats present, highest quality wins."""
        from rubetunes.providers.monochrome.manifest import formats_to_quality
        assert formats_to_quality(["FLAC_HIRES", "FLAC", "AACLC"]) == "HI_RES_LOSSLESS"

    def test_formats_to_quality_empty(self) -> None:
        from rubetunes.providers.monochrome.manifest import formats_to_quality
        assert formats_to_quality([]) is None


# ===========================================================================
# 4. Model construction
# ===========================================================================

class TestModels:
    def test_track_from_dict_basic(self) -> None:
        from rubetunes.providers.monochrome.models import Track
        d = {
            "id": 123,
            "title": "Test Song",
            "duration": 240,
            "artists": [{"id": 1, "name": "Artist A", "type": "MAIN", "handle": None, "picture": None}],
            "album": {"id": 10, "title": "Album X", "cover": "abc-def", "vibrantColor": "#ff0"},
            "audioQuality": "LOSSLESS",
            "isrc": "USUM71703861",
        }
        track = Track.from_dict(d)
        assert track.id == 123
        assert track.title == "Test Song"
        assert track.artist_names == "Artist A"
        assert track.isrc == "USUM71703861"
        assert track.audio_quality == "LOSSLESS"

    def test_track_display_title_with_version(self) -> None:
        from rubetunes.providers.monochrome.models import Track
        track = Track(id=1, title="Song", version="Remastered")
        assert track.display_title == "Song (Remastered)"

    def test_track_display_title_no_version(self) -> None:
        from rubetunes.providers.monochrome.models import Track
        track = Track(id=1, title="Song")
        assert track.display_title == "Song"

    def test_album_from_dict(self) -> None:
        from rubetunes.providers.monochrome.models import Album
        d = {
            "id": 99,
            "title": "My Album",
            "numberOfTracks": 10,
            "releaseDate": "2024-01-01",
            "cover": "uuid-cover",
        }
        album = Album.from_dict(d)
        assert album.id == 99
        assert album.number_of_tracks == 10

    def test_playlist_cover_id(self) -> None:
        from rubetunes.providers.monochrome.models import Playlist
        p = Playlist(uuid="abc", title="My Playlist", square_image="sq-img", image="img")
        assert p.cover_id == "sq-img"
