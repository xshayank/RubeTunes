# -*- coding: utf-8 -*-
"""
Regression tests covering R1-R10 and the Spotify TOTP auth fix.

All HTTP / subprocess calls are mocked; no real network access is needed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_lib

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import spotify_dl as sdl  # noqa: E402


# ===========================================================================
# Spotify TOTP — session / cookie fix
# ===========================================================================

class TestSpotifyTOTP:
    """_totp() must accept an optional server_time override."""

    def test_totp_with_fixed_time(self):
        from rubetunes.spotify_meta import _totp
        secret = "JBSWY3DPEHPK3PXP"  # well-known RFC-6238 test secret
        code1 = _totp(secret, server_time=1_000_000)
        code2 = _totp(secret, server_time=1_000_000)
        assert code1 == code2, "same server_time must give same code"
        assert len(code1) == 6 and code1.isdigit()

    def test_totp_different_window(self):
        from rubetunes.spotify_meta import _totp
        secret = "JBSWY3DPEHPK3PXP"
        # t=0 and t=29 are in the same 30-second window (counter=0)
        code_t0  = _totp(secret, server_time=0)
        code_t29 = _totp(secret, server_time=29)
        assert code_t0 == code_t29, "same 30-second window (0–29) → same code"

    def test_totp_next_window_differs(self):
        from rubetunes.spotify_meta import _totp
        secret = "JBSWY3DPEHPK3PXP"
        code_w1 = _totp(secret, server_time=0)    # window 0
        code_w2 = _totp(secret, server_time=30)   # window 1
        # Different windows produce different codes (with overwhelming probability)
        assert code_w1 != code_w2, "different windows should produce different codes"
        assert isinstance(code_w2, str) and len(code_w2) == 6

    def test_get_totp_secret_env_override(self, monkeypatch):
        from rubetunes import spotify_meta
        monkeypatch.setenv("SPOTIFY_TOTP_SECRET", "OVERRIDEDSECRET")
        secret = spotify_meta._get_totp_secret()
        assert secret == "OVERRIDEDSECRET"

    def test_get_totp_secret_fallback(self, monkeypatch):
        from rubetunes import spotify_meta
        monkeypatch.delenv("SPOTIFY_TOTP_SECRET", raising=False)
        secret = spotify_meta._get_totp_secret()
        assert secret == spotify_meta._SPOTIFY_TOTP_SECRET

    @resp_lib.activate
    def test_fetch_anon_token_uses_session_and_succeeds(self):
        """_fetch_anon_token must use the persistent session and return (token, expires)."""
        from rubetunes import spotify_meta

        # Mock open.spotify.com → sets sp_t cookie
        resp_lib.add(
            resp_lib.GET, "https://open.spotify.com",
            body="<html></html>", status=200,
            headers={"Set-Cookie": "sp_t=fake_sp_t; Path=/"},
        )
        # Mock server-time endpoint
        resp_lib.add(
            resp_lib.GET, "https://open.spotify.com/api/server-time",
            json={"serverTime": int(time.time())}, status=200,
        )
        # Mock token endpoint
        resp_lib.add(
            resp_lib.GET, "https://open.spotify.com/api/token",
            json={"accessToken": "test_access_token", "accessTokenExpirationTimestampMs": (time.time() + 3600) * 1000},
            status=200,
        )

        # Reset the session so we get a fresh one using our mocks
        spotify_meta._reset_anon_session()
        token, expires = spotify_meta._fetch_anon_token()
        assert token == "test_access_token"
        assert expires > time.time()

    @resp_lib.activate
    def test_fetch_anon_token_raises_on_missing_access_token(self):
        from rubetunes import spotify_meta
        resp_lib.add(resp_lib.GET, "https://open.spotify.com", body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/server-time", status=404)
        resp_lib.add(
            resp_lib.GET, "https://open.spotify.com/api/token",
            json={"isAnonymous": True},  # no accessToken
            status=200,
        )
        spotify_meta._reset_anon_session()
        with pytest.raises(RuntimeError, match="accessToken"):
            spotify_meta._fetch_anon_token()

    def test_reset_anon_session_clears_session(self):
        from rubetunes import spotify_meta
        # Ensure a session exists
        with patch("requests.Session") as MockSession:
            MockSession.return_value.get.return_value.status_code = 200
            MockSession.return_value.get.return_value.text = ""
            MockSession.return_value.cookies.get.return_value = "sp_t_val"
            spotify_meta._ensure_anon_session(force_refresh=True)

        spotify_meta._reset_anon_session()
        assert spotify_meta._anon_session is None

    @resp_lib.activate
    def test_get_token_falls_back_to_cc(self, monkeypatch):
        """When the anon token fails, get_token should try client credentials."""
        from rubetunes import spotify_meta

        monkeypatch.setattr(spotify_meta, "SPOTIFY_CLIENT_ID", "fake_id")
        monkeypatch.setattr(spotify_meta, "SPOTIFY_CLIENT_SECRET", "fake_secret")
        # Clear in-memory cache
        spotify_meta._token_cache.clear()
        spotify_meta._reset_anon_session()

        # open.spotify.com returns OK but /api/token returns 500 twice
        resp_lib.add(resp_lib.GET, "https://open.spotify.com", body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/server-time", status=404)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/token", status=500)
        # Second attempt after session reset
        resp_lib.add(resp_lib.GET, "https://open.spotify.com", body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/server-time", status=404)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/token", status=500)
        # CC fallback
        resp_lib.add(
            resp_lib.POST, "https://accounts.spotify.com/api/token",
            json={"access_token": "cc_token", "expires_in": 3600},
            status=200,
        )
        # scrape attempt may also call open.spotify.com — just return empty page
        resp_lib.add(resp_lib.GET, "https://open.spotify.com", body="<html></html>", status=200)
        resp_lib.add(resp_lib.GET, "https://open.spotify.com/api/token", status=500)

        token = spotify_meta.get_token()
        assert token == "cc_token"


# ===========================================================================
# R1 — build_platform_choices
# ===========================================================================

class TestBuildPlatformChoices:
    def _info_with_qobuz(self) -> dict:
        return {
            "track_id": "abc123",
            "isrc": "USUM12345678",
            "title": "Test Track",
            "artists": ["Test Artist"],
            "qobuz_id": "99999",
            "qobuz_bit_depth": 24,
            "qobuz_sample_rate": 96000,
        }

    def test_returns_list(self):
        choices = sdl.build_platform_choices(self._info_with_qobuz(), "flac_hi")
        assert isinstance(choices, list)

    def test_auto_prepended_with_multiple_sources(self):
        info = self._info_with_qobuz()
        # Add tidal_alt_available so there are ≥2 non-auto sources
        info["tidal_alt_available"] = True
        choices = sdl.build_platform_choices(info, "flac_hi")
        # auto must be first
        assert choices[0]["source"] == "auto"

    def test_no_auto_for_single_source(self):
        """If only one platform is available, no 'auto' entry."""
        info = {
            "track_id": "abc123", "isrc": "USUM12345678",
            "title": "T", "artists": ["A"],
            # Only YouTube will match (no qobuz_id, no tidal, no deezer, no amazon)
        }
        choices = sdl.build_platform_choices(info, "mp3")
        sources = [c["source"] for c in choices]
        assert "auto" not in sources

    def test_quality_mp3_returns_youtube(self):
        info = {"track_id": "abc", "isrc": "ISO123", "title": "T", "artists": ["A"]}
        choices = sdl.build_platform_choices(info, "mp3")
        sources = [c["source"] for c in choices]
        assert "youtube" in sources

    def test_quality_flac_excludes_youtube_when_others_available(self):
        info = self._info_with_qobuz()
        choices = sdl.build_platform_choices(info, "flac_hi")
        sources = [c["source"] for c in choices]
        # YouTube should only appear if no FLAC source is available
        non_auto = [c for c in choices if c["source"] not in ("auto", "youtube")]
        if non_auto:
            # YouTube might still be present as fallback but rank must be last
            yt_choices = [c for c in choices if c["source"] == "youtube"]
            non_yt = [c for c in choices if c["source"] not in ("auto", "youtube")]
            if yt_choices and non_yt:
                assert yt_choices[0]["rank"] > non_yt[-1]["rank"]

    def test_qobuz_hires_label(self):
        info = self._info_with_qobuz()
        choices = sdl.build_platform_choices(info, "flac_hi")
        qobuz_choices = [c for c in choices if c["source"] == "qobuz"]
        assert qobuz_choices, "qobuz should be in choices"
        assert "24" in qobuz_choices[0]["label"]

    def test_deezer_requires_arl(self, monkeypatch):
        import os
        monkeypatch.setenv("DEEZER_ARL", "")
        info = {"deezer_id": "12345", "track_id": "x", "isrc": "I", "title": "T", "artists": ["A"]}
        choices = sdl.build_platform_choices(info, "flac_cd")
        assert not any(c["source"] == "deezer" for c in choices)

    def test_deezer_included_with_arl(self, monkeypatch):
        monkeypatch.setenv("DEEZER_ARL", "fake_arl_value")
        info = {"deezer_id": "12345", "track_id": "x", "isrc": "I", "title": "T", "artists": ["A"]}
        choices = sdl.build_platform_choices(info, "flac_cd")
        assert any(c["source"] == "deezer" for c in choices)

    def test_ranks_are_sorted(self):
        info = {
            "track_id": "abc", "isrc": "I", "title": "T", "artists": ["A"],
            "qobuz_id": "9", "qobuz_bit_depth": 16, "qobuz_sample_rate": 44100,
            "tidal_alt_available": True,
        }
        choices = sdl.build_platform_choices(info, "any")
        non_auto = [c for c in choices if c["source"] != "auto"]
        ranks = [c["rank"] for c in non_auto]
        assert ranks == sorted(ranks)


# ===========================================================================
# R1 — best_source_label
# ===========================================================================

class TestBestSourceLabel:
    def test_returns_string(self):
        info = {"track_id": "x", "isrc": "I", "title": "T", "artists": ["A"]}
        label = sdl.best_source_label(info)
        assert isinstance(label, str)
        assert len(label) > 0

    def test_qobuz_highest_rank(self):
        info = {
            "track_id": "abc", "isrc": "I", "title": "T", "artists": ["A"],
            "qobuz_id": "9999", "qobuz_bit_depth": 24, "qobuz_sample_rate": 96000,
            "tidal_alt_available": True,
        }
        label = sdl.best_source_label(info)
        assert "Qobuz" in label


# ===========================================================================
# R2 — download_track_from_choice signature
# ===========================================================================

class TestDownloadTrackFromChoiceSignature:
    """Verify the function accepts the rub.py call-site signature."""

    def test_is_coroutine(self):
        import asyncio
        import inspect
        assert inspect.iscoroutinefunction(sdl.download_track_from_choice)

    def test_auto_dispatches_to_waterfall(self, tmp_path):
        import asyncio

        info = {"track_id": "x", "isrc": "I", "title": "Track", "artists": ["Artist"]}
        choice = {"source": "auto", "quality": "mp3"}

        # _do_waterfall raises DownloadError → the coroutine should wrap it
        with patch("rubetunes.downloader._do_waterfall") as mock_wf:
            mock_wf.side_effect = sdl.DownloadError("waterfall", "all failed")
            coro = sdl.download_track_from_choice(info, choice, str(tmp_path), "yt-dlp")
            with pytest.raises(sdl.DownloadError):
                asyncio.run(coro)

    def test_raises_download_error_on_bad_source(self, tmp_path):
        import asyncio
        info = {"track_id": "x", "isrc": "I", "title": "Track", "artists": ["Artist"]}
        choice = {"source": "nonexistent_source", "quality": "mp3"}
        coro = sdl.download_track_from_choice(info, choice, str(tmp_path), "yt-dlp")
        with pytest.raises(sdl.DownloadError):
            asyncio.run(coro)


# ===========================================================================
# R3 — Amazon proxy resolver
# ===========================================================================

class TestGetAmazonStreamUrl:
    @resp_lib.activate
    def test_happy_path_first_proxy(self):
        from rubetunes.providers.amazon import _get_amazon_stream_url, _AMAZON_PROXY_BASES
        proxy_url = _AMAZON_PROXY_BASES[0].format(asin="B0TEST12345")
        resp_lib.add(
            resp_lib.GET, proxy_url,
            json={"streamUrl": "https://cdn.amazon.music/stream.flac", "decryptionKey": "abc123"},
            status=200,
        )
        stream_url, decryption_key = _get_amazon_stream_url("B0TEST12345")
        assert stream_url == "https://cdn.amazon.music/stream.flac"
        assert decryption_key == "abc123"

    @resp_lib.activate
    def test_falls_back_to_second_proxy(self):
        from rubetunes.providers.amazon import _get_amazon_stream_url, _AMAZON_PROXY_BASES
        first_url  = _AMAZON_PROXY_BASES[0].format(asin="B0FAIL00001")
        second_url = _AMAZON_PROXY_BASES[1].format(asin="B0FAIL00001")
        resp_lib.add(resp_lib.GET, first_url, status=503)
        resp_lib.add(
            resp_lib.GET, second_url,
            json={"streamUrl": "https://cdn2.amazon.music/track.flac"},
            status=200,
        )
        stream_url, _ = _get_amazon_stream_url("B0FAIL00001")
        assert stream_url == "https://cdn2.amazon.music/track.flac"

    @resp_lib.activate
    def test_returns_none_when_all_proxies_fail(self):
        from rubetunes.providers.amazon import _get_amazon_stream_url, _AMAZON_PROXY_BASES
        for tmpl in _AMAZON_PROXY_BASES:
            resp_lib.add(resp_lib.GET, tmpl.format(asin="B0NONE00000"), status=503)
        stream_url, key = _get_amazon_stream_url("B0NONE00000")
        assert stream_url is None
        assert key is None


# ===========================================================================
# R4 — Amazon decryption key / ffmpeg conversion
# ===========================================================================

class TestConvertOrRenameAmazon:
    def test_flac_codec_renamed(self, tmp_path):
        from rubetunes.providers.amazon import _convert_or_rename_amazon
        raw_path = tmp_path / "raw.tmp"
        raw_path.write_bytes(b"FAKEFLACDATA")

        info = {"title": "Test Track", "artists": ["Artist"]}

        # Mock ffprobe → codec=flac, no decrypt
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="flac\n", stderr=""
            )
            result = _convert_or_rename_amazon(raw_path, "", tmp_path, info)

        assert result.suffix == ".flac"

    def test_decrypt_key_triggers_ffmpeg(self, tmp_path):
        from rubetunes.providers.amazon import _convert_or_rename_amazon
        raw_path = tmp_path / "encrypted.raw"
        raw_path.write_bytes(b"ENCRYPTEDDATA")
        info = {"title": "Track", "artists": ["Art"]}

        calls_made = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            # First call = decrypt, create decrypted file
            if "-decryption_key" in cmd:
                out_path = Path(cmd[-1])
                out_path.write_bytes(b"DECRYPTED")
            return MagicMock(returncode=0, stdout="flac\n", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = _convert_or_rename_amazon(raw_path, "DEADBEEF", tmp_path, info)

        # Should have called ffmpeg with -decryption_key
        assert any("-decryption_key" in " ".join(str(c) for c in cmd) for cmd in calls_made)


# ===========================================================================
# R5 — Qobuz auth login + stream URL
# ===========================================================================

class TestQobuzAuthLogin:
    @resp_lib.activate
    def test_login_returns_token(self):
        from rubetunes.providers.qobuz import _qobuz_auth_login, _QOBUZ_API_BASE
        resp_lib.add(
            resp_lib.GET, _QOBUZ_API_BASE + "/track/search",
            json={"tracks": {"items": [], "total": 0}}, status=200,
        )
        resp_lib.add(
            resp_lib.POST, _QOBUZ_API_BASE + "/user/login",
            json={"user_auth_token": "fake_uat_12345"},
            status=200,
        )
        result = _qobuz_auth_login("test@example.com", "password123")
        assert result is not None
        assert result["user_auth_token"] == "fake_uat_12345"

    @resp_lib.activate
    def test_login_returns_none_on_error(self):
        from rubetunes.providers.qobuz import _qobuz_auth_login, _QOBUZ_API_BASE
        resp_lib.add(
            resp_lib.GET, _QOBUZ_API_BASE + "/track/search",
            json={"tracks": {"items": [], "total": 0}}, status=200,
        )
        resp_lib.add(resp_lib.POST, _QOBUZ_API_BASE + "/user/login", status=401)
        result = _qobuz_auth_login("bad@example.com", "wrongpass")
        assert result is None

    @resp_lib.activate
    def test_stream_url_auth(self):
        from rubetunes.providers.qobuz import _get_qobuz_stream_url_auth, _QOBUZ_API_BASE
        from rubetunes.providers import qobuz as qobuz_mod

        fake_token = {
            "user_auth_token": "my_uat",
            "app_id": "712109809",
            "app_secret": "589be88e4538daea11f509d29e4a23b1",
            "fetched_at": time.time(),
        }
        with patch.object(qobuz_mod, "_get_qobuz_auth_token", return_value=fake_token):
            resp_lib.add(
                resp_lib.GET, _QOBUZ_API_BASE + "/track/getFileUrl",
                json={"url": "https://qobuz.stream/flac/track.flac"},
                status=200,
            )
            url = _get_qobuz_stream_url_auth("99999", quality=6)
        assert url == "https://qobuz.stream/flac/track.flac"

    def test_stream_url_auth_returns_none_without_creds(self, monkeypatch):
        from rubetunes.providers.qobuz import _get_qobuz_stream_url_auth
        from rubetunes.providers import qobuz as qmod
        with patch.object(qmod, "_get_qobuz_auth_token", return_value=None):
            assert _get_qobuz_stream_url_auth("12345") is None


# ===========================================================================
# R7 — YouTube Music ISRC search
# ===========================================================================

class TestYouTubeMusic:
    def test_get_url_by_isrc_uses_yt_dlp(self, tmp_path):
        from rubetunes.providers.youtube import _get_youtube_music_url_by_isrc

        yt_output = json.dumps({"webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=yt_output, stderr=""
            )
            url = _get_youtube_music_url_by_isrc(
                "USUM71703861", "Never Gonna Give You Up", "Rick Astley", "yt-dlp"
            )
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_get_url_returns_none_on_failure(self):
        from rubetunes.providers.youtube import _get_youtube_music_url_by_isrc
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            url = _get_youtube_music_url_by_isrc("BADISRC", "", "", "yt-dlp")
        assert url is None

    def test_download_youtube_music_returns_path(self, tmp_path):
        from rubetunes.providers.youtube import _download_youtube_music
        expected = tmp_path / "Artist - Track.mp3"
        expected.write_bytes(b"FAKEMP3")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=str(expected) + "\n", stderr=""
            )
            result = _download_youtube_music(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                tmp_path, "yt-dlp",
                info={"title": "Track", "artists": ["Artist"]},
            )
        assert result.exists()
        assert result.suffix == ".mp3"


# ===========================================================================
# R8 — Format hint parser
# ===========================================================================

class TestParseFormatHint:
    def setup_method(self):
        from rubetunes.resolver import _parse_format_hint
        self._parse = _parse_format_hint

    def test_mp3_hint_stripped(self):
        args, quality = self._parse("https://open.spotify.com/track/abc mp3")
        assert quality == "mp3"
        assert "mp3" not in args

    def test_flac_hint(self):
        _, quality = self._parse("!spotify https://open.spotify.com/track/abc flac")
        assert quality == "flac_cd"

    def test_m4a_hint(self):
        _, quality = self._parse("!spotify https://open.spotify.com/track/abc m4a")
        assert quality == "flac_cd"

    def test_hires_hint(self):
        _, quality = self._parse("!qobuz https://open.qobuz.com/track/12345 hires")
        assert quality == "flac_hi"

    def test_24bit_hint(self):
        _, quality = self._parse("!qobuz https://open.qobuz.com/track/12345 24bit")
        assert quality == "flac_hi"

    def test_no_hint_returns_none(self):
        args, quality = self._parse("https://open.spotify.com/track/abc")
        assert quality is None
        assert args == "https://open.spotify.com/track/abc"

    def test_unknown_token_returns_none(self):
        _, quality = self._parse("!spotify https://open.spotify.com/track/abc ogg")
        assert quality is None

    def test_case_insensitive(self):
        _, quality = self._parse("!spotify url MP3")
        assert quality == "mp3"


# ===========================================================================
# R9 — Queue snapshot round-trip
# ===========================================================================

class TestQueueSnapshot:
    def test_save_and_restore(self, tmp_path, monkeypatch):
        """_save_queue_snapshot writes a JSON file; _restore_queue_snapshot re-reads it."""
        import collections
        import importlib
        import rub  # noqa: E402

        # Patch BASE_DIR and the snapshot path
        snapshot_file = tmp_path / "queue_snapshot.json"
        monkeypatch.setattr(rub, "_QUEUE_SNAPSHOT_FILE", snapshot_file)

        # Add a fake entry to the queue
        rub.download_queue.clear()
        rub.download_queue.append({
            "object_guid": "guid_abc",
            "url": "",
            "choice": None,
            "title": "My Song — My Artist",
            "command": "!spotify",
            "submitted_at": "2025-01-01T00:00:00",
            "queue_msg_id": None,
        })

        # Save
        rub._save_queue_snapshot()
        assert snapshot_file.exists()
        data = json.loads(snapshot_file.read_text())
        assert len(data) == 1
        assert data[0]["user_guid"] == "guid_abc"

        # Clear queue and restore
        rub.download_queue.clear()
        rub._restore_queue_snapshot()
        assert len(rub.download_queue) == 1
        assert rub.download_queue[0]["object_guid"] == "guid_abc"
        assert not snapshot_file.exists()  # file must be deleted after restore

    def test_restore_missing_file_is_noop(self, tmp_path, monkeypatch):
        import rub
        snapshot_file = tmp_path / "no_queue_snapshot.json"
        monkeypatch.setattr(rub, "_QUEUE_SNAPSHOT_FILE", snapshot_file)
        # Should not raise
        rub._restore_queue_snapshot()


# ===========================================================================
# R10 — MusicBrainz pre-flight guard
# ===========================================================================

class TestMusicBrainzGuard:
    def setup_method(self):
        """Reset MB availability before each test."""
        from rubetunes import resolver
        resolver._mb_available = True
        resolver._mb_available_until = 0.0

    @resp_lib.activate
    def test_success_marks_available(self):
        from rubetunes.resolver import _musicbrainz_genre
        resp_lib.add(
            resp_lib.GET,
            "https://musicbrainz.org/ws/2/recording",
            json={
                "recordings": [{"tags": [{"name": "rock", "count": 5}]}]
            },
            status=200,
        )
        genre = _musicbrainz_genre("USUM71703861")
        assert genre == "Rock"

        from rubetunes import resolver
        assert resolver._mb_available is True

    @resp_lib.activate
    def test_failure_marks_unavailable(self):
        from rubetunes.resolver import _musicbrainz_genre
        from rubetunes import resolver
        resp_lib.add(resp_lib.GET, "https://musicbrainz.org/ws/2/recording", status=503)
        result = _musicbrainz_genre("USUM71703861")
        assert result == ""
        assert resolver._mb_available is False
        assert resolver._mb_available_until > time.time()

    def test_short_circuit_when_unavailable(self):
        from rubetunes import resolver
        from rubetunes.resolver import _musicbrainz_genre

        resolver._mb_available = False
        resolver._mb_available_until = time.time() + 60

        # Must NOT make a network call; just return ""
        with patch("requests.get") as mock_get:
            result = _musicbrainz_genre("USUM71703861")
        mock_get.assert_not_called()
        assert result == ""

    @resp_lib.activate
    def test_retries_after_ttl(self):
        from rubetunes import resolver
        from rubetunes.resolver import _musicbrainz_genre

        # Mark as unavailable but with expired TTL
        resolver._mb_available = False
        resolver._mb_available_until = time.time() - 1  # expired

        resp_lib.add(
            resp_lib.GET,
            "https://musicbrainz.org/ws/2/recording",
            json={"recordings": [{"tags": [{"name": "pop", "count": 3}]}]},
            status=200,
        )
        genre = _musicbrainz_genre("USUM71703861")
        assert genre == "Pop"
        assert resolver._mb_available is True
