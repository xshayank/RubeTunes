# -*- coding: utf-8 -*-
"""Tests for the SpotiFLAC port:

1. TOTPGenerator known-vector test — pins timestamp 1_700_000_000 to its
   expected 6-digit code so we can verify parity with SpotiFLAC.
2. GraphQL request-builder tests — verify that all pathfinder queries use the
   correct URL/headers and that ``api.spotify.com`` is *never* called.

All HTTP calls are mocked; no real network access is needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import responses as resp_lib

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ===========================================================================
# 1 — TOTP known-vector (pins timestamp → code for parity with SpotiFLAC)
# ===========================================================================

class TestTOTPKnownVector:
    """The known-vector pair was computed once from the TOTP algorithm and
    pinned here.  If this test fails the TOTP implementation has drifted from
    SpotiFLAC's pquerna/otp behaviour."""

    def test_known_vector_matches_module_constant(self):
        """KNOWN_VECTOR_CODE == _totp_raw(secret, 1_700_000_000)."""
        from rubetunes.spotify.totp import (
            TOTPGenerator,
            SPOTIFY_TOTP_SECRET,
            KNOWN_VECTOR_TS,
            KNOWN_VECTOR_CODE,
            _totp_raw,
        )
        computed = _totp_raw(SPOTIFY_TOTP_SECRET, KNOWN_VECTOR_TS)
        assert computed == KNOWN_VECTOR_CODE, (
            f"TOTP drift detected: computed={computed!r}, constant={KNOWN_VECTOR_CODE!r}"
        )

    def test_known_vector_is_six_digits(self):
        from rubetunes.spotify.totp import KNOWN_VECTOR_CODE
        assert len(KNOWN_VECTOR_CODE) == 6
        assert KNOWN_VECTOR_CODE.isdigit()

    def test_known_vector_generator_class(self):
        """TOTPGenerator.generate(ts=KNOWN_VECTOR_TS) == KNOWN_VECTOR_CODE."""
        from rubetunes.spotify.totp import (
            TOTPGenerator,
            KNOWN_VECTOR_TS,
            KNOWN_VECTOR_CODE,
        )
        gen = TOTPGenerator()
        assert gen.generate(ts=KNOWN_VECTOR_TS) == KNOWN_VECTOR_CODE

    def test_known_vector_generate_totp_helper(self):
        """generate_totp(ts=KNOWN_VECTOR_TS) == KNOWN_VECTOR_CODE."""
        from rubetunes.spotify.totp import (
            generate_totp,
            KNOWN_VECTOR_TS,
            KNOWN_VECTOR_CODE,
        )
        assert generate_totp(ts=KNOWN_VECTOR_TS) == KNOWN_VECTOR_CODE

    def test_totp_same_window_same_code(self):
        """Timestamps within the same 30-s window must produce the same code."""
        from rubetunes.spotify.totp import _totp_raw, SPOTIFY_TOTP_SECRET
        # Use a window-aligned base: 1_700_000_000 // 30 * 30 = 1_699_999_980
        ts_base = 1_699_999_980  # exact window boundary
        code_a  = _totp_raw(SPOTIFY_TOTP_SECRET, ts_base)
        code_b  = _totp_raw(SPOTIFY_TOTP_SECRET, ts_base + 29)
        assert code_a == code_b

    def test_totp_next_window_differs(self):
        """Adjacent 30-s windows must (with overwhelming probability) differ."""
        from rubetunes.spotify.totp import _totp_raw, SPOTIFY_TOTP_SECRET
        ts_base = 1_700_000_000
        code_a  = _totp_raw(SPOTIFY_TOTP_SECRET, ts_base)
        code_b  = _totp_raw(SPOTIFY_TOTP_SECRET, ts_base + 30)
        assert code_a != code_b

    def test_generate_with_version(self):
        """generate_with_version() returns (code, 61)."""
        from rubetunes.spotify.totp import (
            TOTPGenerator,
            SPOTIFY_TOTP_VERSION,
            KNOWN_VECTOR_TS,
            KNOWN_VECTOR_CODE,
        )
        gen  = TOTPGenerator()
        code, ver = gen.generate_with_version(ts=KNOWN_VECTOR_TS)
        assert code == KNOWN_VECTOR_CODE
        assert ver  == SPOTIFY_TOTP_VERSION


# ===========================================================================
# 2 — GraphQL request-builder policy tests
#     Verify correct URL, headers, and that api.spotify.com is NEVER hit.
# ===========================================================================

class TestGraphQLRequestBuilders:
    """All pathfinder queries must go to api-partner.spotify.com, never to
    api.spotify.com.  We intercept requests.get / requests.post via the
    ``responses`` library and assert on the intercepted URLs and headers."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _graphql_ok_response() -> dict:
        return {"data": {}}

    @staticmethod
    def _register_pathfinder_v1(rsps: "resp_lib.RequestsMock") -> None:
        rsps.add(
            resp_lib.GET,
            "https://api-partner.spotify.com/pathfinder/v1/query",
            json={"data": {}},
            status=200,
        )

    # ------------------------------------------------------------------
    # getTrack
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_get_track_uses_pathfinder_v1(self):
        """_fetch_track_graphql must call pathfinder/v1/query, not api.spotify.com."""
        self._register_pathfinder_v1(resp_lib)

        # Provide a fake token so _auth_headers() doesn't try to fetch a real one
        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_track_graphql
            try:
                _fetch_track_graphql("3n3Ppam7vgaVa1iaRUIOIE")
            except Exception:
                pass  # we only care about the URL hit, not the parse result

        assert len(resp_lib.calls) >= 1
        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    @resp_lib.activate
    def test_get_track_sends_authorization_header(self):
        """The Authorization: Bearer header must be present."""
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="test_token_abc"):
            from rubetunes.spotify_meta import _fetch_track_graphql
            try:
                _fetch_track_graphql("3n3Ppam7vgaVa1iaRUIOIE")
            except Exception:
                pass

        auth = resp_lib.calls[0].request.headers.get("Authorization", "")
        assert auth == "Bearer test_token_abc"

    # ------------------------------------------------------------------
    # getAlbum
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_get_album_uses_pathfinder_v1(self):
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_album_graphql_page
            try:
                _fetch_album_graphql_page("1DFixLWuPkv3KT3TnV35m3", 0, 50)
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    # ------------------------------------------------------------------
    # fetchPlaylist
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_fetch_playlist_uses_pathfinder_v1(self):
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_playlist_graphql_page
            try:
                _fetch_playlist_graphql_page("37i9dQZF1DXcBWIGoYBM5M", 0, 100)
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    # ------------------------------------------------------------------
    # queryArtistOverview
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_get_artist_uses_pathfinder_v1(self):
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_artist_overview_graphql
            try:
                _fetch_artist_overview_graphql("3WrFJ7ztbogyGnTHbHJFl2")
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    # ------------------------------------------------------------------
    # queryArtistDiscographyAll
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_get_artist_discography_uses_pathfinder_v1(self):
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_artist_discography_graphql
            try:
                _fetch_artist_discography_graphql("3WrFJ7ztbogyGnTHbHJFl2", 0, 50)
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    # ------------------------------------------------------------------
    # searchDesktop
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_search_uses_pathfinder_v1(self):
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_search_graphql
            try:
                _fetch_search_graphql("Bohemian Rhapsody", 0, 10)
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "api-partner.spotify.com/pathfinder/v1/query" in url
        assert "api.spotify.com" not in url

    # ------------------------------------------------------------------
    # High-level helpers — assert api.spotify.com is NEVER called
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_spotify_search_never_calls_api_spotify_com(self):
        """spotify_search() must NOT hit api.spotify.com under any code path."""
        # Register pathfinder v1 (what should be called)
        resp_lib.add(
            resp_lib.GET,
            "https://api-partner.spotify.com/pathfinder/v1/query",
            json={"data": {"searchV2": {"tracksV2": {"items": []}}}},
            status=200,
        )
        # If api.spotify.com is called, we want to detect it
        resp_lib.add(
            resp_lib.GET,
            "https://api.spotify.com/v1/search",
            json={"error": "this endpoint should not be called"},
            status=400,
        )

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import spotify_search
            results = spotify_search("test query", limit=5)

        for c in resp_lib.calls:
            assert "api.spotify.com" not in c.request.url, (
                f"Forbidden endpoint was called: {c.request.url}"
            )

    @resp_lib.activate
    def test_get_artist_info_never_calls_api_spotify_com(self):
        """get_spotify_artist_info() must NOT hit api.spotify.com."""
        resp_lib.add(
            resp_lib.GET,
            "https://api-partner.spotify.com/pathfinder/v1/query",
            json={"data": {"artistUnion": {}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            "https://api.spotify.com/v1/artists/3WrFJ7ztbogyGnTHbHJFl2",
            json={"error": "forbidden"},
            status=400,
        )

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import get_spotify_artist_info
            try:
                get_spotify_artist_info("3WrFJ7ztbogyGnTHbHJFl2")
            except Exception:
                pass

        for c in resp_lib.calls:
            assert "api.spotify.com" not in c.request.url, (
                f"Forbidden endpoint was called: {c.request.url}"
            )

    @resp_lib.activate
    def test_get_artist_albums_never_calls_api_spotify_com(self):
        """get_spotify_artist_albums() must NOT hit api.spotify.com."""
        resp_lib.add(
            resp_lib.GET,
            "https://api-partner.spotify.com/pathfinder/v1/query",
            json={"data": {"artistUnion": {"discography": {"all": {"items": [], "totalCount": 0}}}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            "https://api.spotify.com/v1/artists/3WrFJ7ztbogyGnTHbHJFl2/albums",
            json={"error": "forbidden"},
            status=400,
        )

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import get_spotify_artist_albums
            try:
                get_spotify_artist_albums("3WrFJ7ztbogyGnTHbHJFl2", "all", 0, 20)
            except Exception:
                pass

        for c in resp_lib.calls:
            assert "api.spotify.com" not in c.request.url, (
                f"Forbidden endpoint was called: {c.request.url}"
            )

    # ------------------------------------------------------------------
    # Verify correct operation names in payload
    # ------------------------------------------------------------------

    @resp_lib.activate
    def test_get_track_operation_name(self):
        """_fetch_track_graphql must send operationName=getTrack."""
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_track_graphql
            try:
                _fetch_track_graphql("3n3Ppam7vgaVa1iaRUIOIE")
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "operationName=getTrack" in url

    @resp_lib.activate
    def test_search_operation_name(self):
        """_fetch_search_graphql must send operationName=searchDesktop."""
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_search_graphql
            try:
                _fetch_search_graphql("Queen", 0, 5)
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "operationName=searchDesktop" in url

    @resp_lib.activate
    def test_artist_overview_operation_name(self):
        """_fetch_artist_overview_graphql must send operationName=queryArtistOverview."""
        self._register_pathfinder_v1(resp_lib)

        with patch("rubetunes.spotify_meta.get_token", return_value="fake_bearer"):
            from rubetunes.spotify_meta import _fetch_artist_overview_graphql
            try:
                _fetch_artist_overview_graphql("3WrFJ7ztbogyGnTHbHJFl2")
            except Exception:
                pass

        url = resp_lib.calls[0].request.url
        assert "operationName=queryArtistOverview" in url
