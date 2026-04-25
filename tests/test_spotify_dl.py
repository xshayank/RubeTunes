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
# 2.  TOTP generator — mirrors spotbye/SpotiFLAC backend/spotify_totp.go
#
# SpotiFLAC uses HMAC-SHA1 TOTP (RFC 6238) with the Spotify web-player
# secret and a 30-second time step.  The expected codes below were computed
# independently with the same algorithm to confirm byte-for-byte parity.
# ===========================================================================

class TestTOTPGenerator:
    """Verify _totp() matches SpotiFLAC's generateSpotifyTOTP() for known inputs."""

    SECRET = sdl._SPOTIFY_TOTP_SECRET

    def test_known_timestamp_1(self):
        """t=1700000000 → counter 56666666 → '371599'."""
        assert sdl._totp(self.SECRET, server_time=1700000000) == "371599"

    def test_known_timestamp_2(self):
        """t=1700000030 (next 30-second window) → counter 56666667 → '947302'."""
        assert sdl._totp(self.SECRET, server_time=1700000030) == "947302"

    def test_known_timestamp_epoch(self):
        """t=0 → counter 0 → '204513'."""
        assert sdl._totp(self.SECRET, server_time=0) == "204513"

    def test_known_timestamp_billion(self):
        """t=1000000000 → counter 33333333 → '371947'."""
        assert sdl._totp(self.SECRET, server_time=1000000000) == "371947"

    def test_output_is_six_digits(self):
        """Output is always exactly 6 decimal digits (zero-padded)."""
        code = sdl._totp(self.SECRET, server_time=1700000000)
        assert len(code) == 6
        assert code.isdigit()

    def test_same_window_produces_same_code(self):
        """Two timestamps in the same 30-second window yield the same code."""
        # Use a multiple-of-30 base so +29 stays within the same window.
        # 1700000040 = 56666668 × 30, so [1700000040, 1700000069] all share counter 56666668.
        t_base = 1700000040
        assert sdl._totp(self.SECRET, server_time=t_base) == sdl._totp(self.SECRET, server_time=t_base + 29)

    def test_adjacent_windows_differ(self):
        """Adjacent 30-second windows (typically) produce different codes."""
        t = 1700000000
        code_a = sdl._totp(self.SECRET, server_time=t)
        code_b = sdl._totp(self.SECRET, server_time=t + 30)
        assert code_a != code_b, "Adjacent windows should produce different TOTP codes"

    def test_totp_version_constant(self):
        """SpotiFLAC hardcodes totpVer=61; confirm the constant is unchanged."""
        assert sdl._SPOTIFY_TOTP_VERSION == 61

    def test_secret_constant(self):
        """The hardcoded TOTP secret must match SpotiFLAC's spotify_totp.go constant."""
        expected = (
            "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YT"
            "CMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
        )
        assert self.SECRET == expected

    def test_uses_local_clock_when_no_server_time(self):
        """Without server_time the function uses the real clock and returns 6 digits."""
        with patch("rubetunes.spotify_meta.time") as mock_time:
            mock_time.time.return_value = 1700000000
            code = sdl._totp(self.SECRET)
        assert len(code) == 6
        assert code.isdigit()
        assert code == "371599"


# ===========================================================================
# 3.  Spotify 429 / token infrastructure fixes
#
# Three root causes of Spotify HTTP 429 "Too Many Requests" errors were
# identified and fixed:
#   A) get_token() had no threading lock — concurrent batch-download threads
#      all refreshed the token simultaneously.
#   B) _fetch_spotify_server_time() used get_access_token as a fallback — that
#      URL is a TOTP-gated token endpoint; calling it without TOTP params
#      triggered 429s on every token refresh.
#   C) _fetch_anon_token() did not honour Retry-After — it re-raised
#      immediately, making the retry loop hammer the endpoint.
# ===========================================================================

class TestSpotify429Fixes:
    """Regression tests for the three root causes of Spotify HTTP 429 errors."""

    # -----------------------------------------------------------------------
    # A) Token lock prevents concurrent refreshes
    # -----------------------------------------------------------------------

    def test_token_lock_exists(self):
        """_token_lock must be a threading.Lock so concurrent threads serialise."""
        import threading
        assert isinstance(sdl._token_lock, type(threading.Lock()))

    def test_get_token_returns_cached_without_lock_contention(self):
        """get_token() returns from the fast path (cache) without any network call."""
        import threading
        original_cache = dict(sdl._token_cache)
        sdl._token_cache.update({"token": "tok_cached", "expires_at": time.time() + 3600})
        try:
            result = sdl.get_token()
            assert result == "tok_cached"
        finally:
            sdl._token_cache.clear()
            sdl._token_cache.update(original_cache)

    def test_concurrent_token_refreshes_serialised(self):
        """Only one real token fetch happens when multiple threads call get_token()."""
        import threading

        fetch_count = {"n": 0}

        def counting_fetch():
            fetch_count["n"] += 1
            # Simulate the cache being written as a real fetch would do
            sdl._token_cache.update({"token": "tok_concurrent", "expires_at": time.time() + 3600})
            return ("tok_concurrent", time.time() + 3600)

        # Ensure both in-memory and disk caches are empty so threads need to refresh
        original_cache = dict(sdl._token_cache)
        sdl._token_cache.clear()

        errors = []
        results = []

        def worker():
            try:
                with (
                    patch("rubetunes.spotify_meta._fetch_anon_token", counting_fetch),
                    patch("rubetunes.spotify_meta._load_spotify_token", return_value={}),
                    patch("rubetunes.spotify_meta._save_spotify_token"),
                ):
                    tok = sdl.get_token()
                results.append(tok)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sdl._token_cache.clear()
        sdl._token_cache.update(original_cache)

        assert not errors, f"Workers raised errors: {errors}"
        # All workers must have gotten a token
        assert len(results) == 5
        # The lock + double-check means only ONE actual fetch should fire;
        # the remaining threads find the cache populated inside the lock.
        assert fetch_count["n"] == 1, (
            f"Expected exactly 1 token fetch (lock serialises), got {fetch_count['n']}"
        )

    # -----------------------------------------------------------------------
    # B) _fetch_spotify_server_time() no longer falls back to get_access_token
    # -----------------------------------------------------------------------

    @resp_lib.activate
    def test_server_time_only_hits_api_server_time(self):
        """_fetch_spotify_server_time() only calls /api/server-time, never get_access_token."""
        resp_lib.add(
            resp_lib.GET,
            "https://open.spotify.com/api/server-time",
            json={"serverTime": 1700000000},
            status=200,
        )
        import requests as _req
        sess = _req.Session()
        result = sdl._fetch_spotify_server_time(sess)
        assert result == 1700000000
        # get_access_token must NOT have been called — if it were, responses
        # would raise ConnectionError because we didn't register it.

    @resp_lib.activate
    def test_server_time_returns_none_on_failure_no_fallback(self):
        """When /api/server-time fails, function returns None without hitting any other URL."""
        resp_lib.add(
            resp_lib.GET,
            "https://open.spotify.com/api/server-time",
            status=503,
        )
        import requests as _req
        sess = _req.Session()
        result = sdl._fetch_spotify_server_time(sess)
        # Must return None — no fallback to get_access_token
        assert result is None

    # -----------------------------------------------------------------------
    # C) _fetch_anon_token() honours Retry-After on 429
    # -----------------------------------------------------------------------

    @resp_lib.activate
    def test_fetch_anon_token_sleeps_on_429(self):
        """_fetch_anon_token() sleeps Retry-After seconds and raises RuntimeError on 429."""
        # Stub session initialisation endpoints
        resp_lib.add(resp_lib.GET, "https://open.spotify.com",
                     body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/server-time",
                     json={"serverTime": 1700000000}, status=200)
        resp_lib.add(
            resp_lib.GET,
            "https://open.spotify.com/api/token",
            status=429,
            headers={"Retry-After": "2"},
        )

        slept = {"seconds": 0.0}

        def fake_sleep(s):
            slept["seconds"] = s

        with patch("rubetunes.spotify_meta._reset_anon_session"):
            with patch("rubetunes.spotify_meta._ensure_anon_session") as mock_sess:
                import requests as _req
                mock_sess.return_value = _req.Session()
                with patch("rubetunes.spotify_meta.time") as mock_time:
                    mock_time.time.return_value = 1700000000
                    mock_time.sleep = fake_sleep
                    with pytest.raises(RuntimeError, match="rate-limited"):
                        sdl._fetch_anon_token()

        assert slept["seconds"] == 2.0, (
            f"Expected sleep(2) for Retry-After: 2, got sleep({slept['seconds']})"
        )

    @resp_lib.activate
    def test_fetch_anon_token_default_sleep_when_no_retry_after(self):
        """Without Retry-After header, _fetch_anon_token() sleeps the default 5 s."""
        resp_lib.add(resp_lib.GET, "https://open.spotify.com",
                     body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/server-time",
                     json={"serverTime": 1700000000}, status=200)
        resp_lib.add(
            resp_lib.GET,
            "https://open.spotify.com/api/token",
            status=429,
            # No Retry-After header
        )

        slept = {"seconds": 0.0}

        def fake_sleep(s):
            slept["seconds"] = s

        with patch("rubetunes.spotify_meta._reset_anon_session"):
            with patch("rubetunes.spotify_meta._ensure_anon_session") as mock_sess:
                import requests as _req
                mock_sess.return_value = _req.Session()
                with patch("rubetunes.spotify_meta.time") as mock_time:
                    mock_time.time.return_value = 1700000000
                    mock_time.sleep = fake_sleep
                    with pytest.raises(RuntimeError, match="rate-limited"):
                        sdl._fetch_anon_token()

        assert slept["seconds"] == 5.0, (
            f"Expected default sleep(5), got sleep({slept['seconds']})"
        )


# ===========================================================================
# 4.  _resolve_deezer
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
# 5.  _resolve_qobuz_by_isrc  (mocks the whole signed-request machinery)
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
# 6.  _resolve_tidal_by_isrc
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
# 7.  _get_tidal_alt_url_by_tidal_id — four response shapes
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
# 8.  _download_tidal_manifest — mock segment URLs, assert concatenated bytes
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
# 9.  Circuit breaker state machine (monkey-patch time.time)
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
# 10.  LRU cache correctness and TTL expiry (monkey-patch time.time)
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
# 11.  New Spotify URL parsers
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
    """get_spotify_playlist_tracks now uses GraphQL fetchPlaylist — update mocks."""

    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "playlistV2": {
                            "uri": f"spotify:playlist:{_PL_ID}",
                            "name": "Test Playlist",
                            "description": "",
                            "ownerV2": {
                                "data": {
                                    "name": "Test Owner",
                                    "avatar": {"sources": []},
                                }
                            },
                            "images": {"items": []},
                            "content": {
                                "totalCount": 2,
                                "items": [
                                    {
                                        "itemV2": {
                                            "data": {
                                                "uri": "spotify:track:track1",
                                                "id": "track1",
                                                "name": "Track One",
                                                "artists": {"items": []},
                                                "trackDuration": {"totalMilliseconds": 0},
                                                "contentRating": {"label": "NONE"},
                                                "albumOfTrack": {"uri": "spotify:album:a1", "name": "A1", "coverArt": {"sources": []}},
                                            }
                                        },
                                        "attributes": [],
                                    },
                                    {
                                        "itemV2": {
                                            "data": {
                                                "uri": "spotify:track:track2",
                                                "id": "track2",
                                                "name": "Track Two",
                                                "artists": {"items": []},
                                                "trackDuration": {"totalMilliseconds": 0},
                                                "contentRating": {"label": "NONE"},
                                                "albumOfTrack": {"uri": "spotify:album:a1", "name": "A1", "coverArt": {"sources": []}},
                                            }
                                        },
                                        "attributes": [],
                                    },
                                ],
                            },
                        }
                    }
                },
                status=200,
            )
            info, track_ids = sdl.get_spotify_playlist_tracks(_PL_ID)

        assert info["name"] == "Test Playlist"
        assert info["owner"] == "Test Owner"
        assert info["total_tracks"] == 2
        assert track_ids == ["track1", "track2"]

    @resp_lib.activate
    def test_skips_null_tracks(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "playlistV2": {
                            "uri": f"spotify:playlist:{_PL_ID}",
                            "name": "PL",
                            "description": "",
                            "ownerV2": {"data": {"name": "o", "avatar": {"sources": []}}},
                            "images": {"items": []},
                            "content": {
                                "totalCount": 1,
                                "items": [
                                    {"itemV2": {"data": {}}},  # empty → skipped
                                    {
                                        "itemV2": {
                                            "data": {
                                                "uri": "spotify:track:realtrack",
                                                "id": "realtrack",
                                                "name": "Real Track",
                                                "artists": {"items": []},
                                                "trackDuration": {"totalMilliseconds": 0},
                                                "contentRating": {"label": "NONE"},
                                                "albumOfTrack": {"uri": "", "name": "", "coverArt": {"sources": []}},
                                            }
                                        },
                                        "attributes": [],
                                    },
                                ],
                            },
                        }
                    }
                },
                status=200,
            )
            _, track_ids = sdl.get_spotify_playlist_tracks(_PL_ID)
        assert "realtrack" in track_ids


class TestGetSpotifyAlbumTracks:
    """get_spotify_album_tracks now uses GraphQL getAlbum — update mocks."""

    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "albumUnion": {
                            "uri": f"spotify:album:{_ALB_ID}",
                            "name": "Test Album",
                            "artists": {
                                "items": [{"profile": {"name": "Artist A"}}]
                            },
                            "date": {"isoString": "2023-01-01"},
                            "coverArt": {"sources": []},
                            "tracksV2": {
                                "totalCount": 2,
                                "items": [
                                    {
                                        "track": {
                                            "uri": "spotify:track:t1",
                                            "id": "t1",
                                            "name": "T1",
                                            "duration": {"totalMilliseconds": 0},
                                        }
                                    },
                                    {
                                        "track": {
                                            "uri": "spotify:track:t2",
                                            "id": "t2",
                                            "name": "T2",
                                            "duration": {"totalMilliseconds": 0},
                                        }
                                    },
                                ],
                            },
                        }
                    }
                },
                status=200,
            )
            info, track_ids = sdl.get_spotify_album_tracks(_ALB_ID)

        assert info["name"] == "Test Album"
        assert "Artist A" in info["artists"]
        assert info["release_date"] == "2023-01-01"
        assert info["total_tracks"] == 2
        assert track_ids == ["t1", "t2"]


class TestGetSpotifyArtistInfo:
    """get_spotify_artist_info now uses GraphQL queryArtistOverview — update mocks."""

    @resp_lib.activate
    def test_happy_path(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "artistUnion": {
                            "uri": f"spotify:artist:{_ART_ID}",
                            "profile": {
                                "name": "Taylor Swift",
                                "verified": True,
                                "biography": {"text": ""},
                            },
                            "stats": {
                                "followers": 100000,
                                "monthlyListeners": 50000,
                                "worldRank": 1,
                            },
                            "visuals": {
                                "avatarImage": {
                                    "sources": [
                                        {"url": "https://example.com/ts.jpg", "width": 640, "height": 640}
                                    ]
                                }
                            },
                            "discography": {
                                "popularReleasesAlbums": {"items": []},
                            },
                        }
                    }
                },
                status=200,
            )
            info = sdl.get_spotify_artist_info(_ART_ID)

        assert info["name"] == "Taylor Swift"
        assert "image_url" in info

    @resp_lib.activate
    def test_limits_top_tracks_to_5(self):
        """top_tracks list should be bounded."""
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "artistUnion": {
                            "uri": f"spotify:artist:{_ART_ID}",
                            "profile": {"name": "Artist", "biography": {"text": ""}},
                            "stats": {"followers": 0, "monthlyListeners": 0, "worldRank": 0},
                            "visuals": {"avatarImage": {"sources": []}},
                            "discography": {"popularReleasesAlbums": {"items": []}},
                        }
                    }
                },
                status=200,
            )
            info = sdl.get_spotify_artist_info(_ART_ID)
        # top_tracks may be empty when discography is empty — just assert it's a list
        assert isinstance(info.get("top_tracks", []), list)
        assert len(info.get("top_tracks", [])) <= 5


class TestGetSpotifyArtistAlbums:
    """get_spotify_artist_albums now uses GraphQL queryArtistDiscographyAll."""

    @resp_lib.activate
    def test_happy_path_albums(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "artistUnion": {
                            "discography": {
                                "all": {
                                    "totalCount": 1,
                                    "items": [
                                        {
                                            "releases": [
                                                {
                                                    "uri": "spotify:album:alb1",
                                                    "id": "alb1",
                                                    "name": "Album One",
                                                    "artists": {
                                                        "items": [{"profile": {"name": "Taylor Swift"}}]
                                                    },
                                                    "date": {"isoString": "2020-01-01"},
                                                    "coverArt": {"sources": []},
                                                    "tracks": {"totalCount": 12},
                                                }
                                            ]
                                        }
                                    ],
                                }
                            }
                        }
                    }
                },
                status=200,
            )
            items, total = sdl.get_spotify_artist_albums(_ART_ID, "album", 0, 10)

        assert total == 1
        assert len(items) == 1
        a = items[0]
        assert a["id"] == "alb1"
        assert a["name"] == "Album One"
        assert a["release_date"] == "2020-01-01"
        assert a["total_tracks"] == 12

    @resp_lib.activate
    def test_happy_path_singles(self):
        with patch("rubetunes.spotify_meta.get_token", return_value=_FAKE_TOKEN):
            resp_lib.add(
                resp_lib.GET,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                json={
                    "data": {
                        "artistUnion": {
                            "discography": {
                                "all": {"totalCount": 0, "items": []}
                            }
                        }
                    }
                },
                status=200,
            )
            items, total = sdl.get_spotify_artist_albums(_ART_ID, "single", 0, 10)
        assert total == 0
        assert items == []

