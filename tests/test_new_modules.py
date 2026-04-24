# -*- coding: utf-8 -*-
"""
Tests for new rubetunes modules: rate_limiter, disk_guard, apple_music provider.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import responses as resp_lib

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ===========================================================================
# Rate limiter (D3)
# ===========================================================================

class TestRateLimiter:
    def setup_method(self):
        """Clear rate limiter state between tests."""
        import rubetunes.rate_limiter as rl
        rl._usage.clear()

    def test_under_limit_allowed(self):
        from rubetunes.rate_limiter import check_rate_limit, record_usage, USER_TRACKS_PER_HOUR
        user = "test_user_1"
        # Record 5 usages — well under limit
        for _ in range(5):
            record_usage(user)
        ok, msg = check_rate_limit(user)
        assert ok
        assert msg == ""

    def test_at_limit_blocked(self):
        from rubetunes.rate_limiter import check_rate_limit, record_usage, USER_TRACKS_PER_HOUR
        import rubetunes.rate_limiter as rl

        user = "test_user_2"
        # Temporarily lower limit for test speed
        original = rl.USER_TRACKS_PER_HOUR
        rl.USER_TRACKS_PER_HOUR = 3
        try:
            for _ in range(3):
                record_usage(user)
            ok, msg = check_rate_limit(user)
            assert not ok
            assert "Rate limit" in msg
            assert "Try again" in msg
        finally:
            rl.USER_TRACKS_PER_HOUR = original

    def test_expired_usages_not_counted(self):
        """Timestamps older than 1 hour should not count."""
        from rubetunes.rate_limiter import check_rate_limit, get_usage_count
        import rubetunes.rate_limiter as rl
        import collections

        user = "test_user_3"
        old_ts = time.time() - 3700  # > 1 hour ago
        with rl._lock:
            rl._usage[user] = collections.deque([old_ts])

        count = get_usage_count(user)
        assert count == 0
        ok, msg = check_rate_limit(user)
        assert ok

    def test_different_users_independent(self):
        from rubetunes.rate_limiter import check_rate_limit, record_usage
        import rubetunes.rate_limiter as rl

        rl.USER_TRACKS_PER_HOUR = 2
        try:
            record_usage("user_a")
            record_usage("user_a")
            ok_a, _ = check_rate_limit("user_a")
            ok_b, _ = check_rate_limit("user_b")
            assert not ok_a
            assert ok_b
        finally:
            rl.USER_TRACKS_PER_HOUR = 100


# ===========================================================================
# Disk space guard (D2)
# ===========================================================================

class TestDiskGuard:
    def test_sufficient_space_allowed(self, tmp_path):
        from rubetunes.disk_guard import check_disk_space

        with patch("rubetunes.disk_guard.shutil.disk_usage") as mock_usage:
            # 10 GB free — way more than enough
            mock_usage.return_value = MagicMock(free=10 * 1024 ** 3)
            ok, msg = check_disk_space(10, tmp_path)
            assert ok
            assert msg == ""

    def test_insufficient_space_rejected(self, tmp_path):
        from rubetunes.disk_guard import check_disk_space

        with patch("rubetunes.disk_guard.shutil.disk_usage") as mock_usage:
            # Only 100 MB free — not enough for 10 FLAC tracks (~300 MB estimated)
            mock_usage.return_value = MagicMock(free=100 * 1024 * 1024)
            ok, msg = check_disk_space(10, tmp_path)
            assert not ok
            assert "disk space" in msg.lower()
            assert "MB" in msg

    def test_disk_usage_error_allows(self, tmp_path):
        """If we can't determine disk usage, let it through."""
        from rubetunes.disk_guard import check_disk_space

        with patch("rubetunes.disk_guard.shutil.disk_usage", side_effect=OSError("permission denied")):
            ok, msg = check_disk_space(5, tmp_path)
            assert ok


# ===========================================================================
# Apple Music provider (C4)
# ===========================================================================

class TestAppleMusicProvider:
    @resp_lib.activate
    def test_enrich_adds_cover(self):
        from rubetunes.providers.apple_music import enrich_from_apple_music

        resp_lib.add(
            resp_lib.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "artworkUrl100": "https://is.mzstatic.com/image/thumb/foo/100x100bb.jpg",
                        "trackNumber": 3,
                        "discNumber": 1,
                        "releaseDate": "2021-06-25T07:00:00Z",
                    }
                ]
            },
            status=200,
        )

        info = {"title": "Bohemian Rhapsody", "artists": ["Queen"], "album": "A Night at the Opera"}
        result = enrich_from_apple_music(info)

        # Cover should be upscaled to 1400x1400
        assert result.get("cover_url")
        assert "1400x1400" in result["cover_url"]

        # Track/disc numbers populated
        assert result.get("track_number") == 3
        assert result.get("disc_number") == 1
        assert result.get("release_date") == "2021-06-25"

    @resp_lib.activate
    def test_enrich_no_results(self):
        from rubetunes.providers.apple_music import enrich_from_apple_music

        resp_lib.add(
            resp_lib.GET,
            "https://itunes.apple.com/search",
            json={"results": []},
            status=200,
        )

        info = {"title": "Unknown Song", "artists": ["Unknown Artist"]}
        result = enrich_from_apple_music(info)
        # Should not raise; just returns unchanged info
        assert result is info

    def test_enrich_missing_title_skipped(self):
        from rubetunes.providers.apple_music import enrich_from_apple_music

        info = {"artists": ["Queen"]}  # no title
        result = enrich_from_apple_music(info)
        assert result is info  # returned unchanged

    @resp_lib.activate
    def test_fetch_apple_cover(self):
        from rubetunes.providers.apple_music import fetch_apple_cover

        resp_lib.add(
            resp_lib.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "artworkUrl100": "https://is.mzstatic.com/image/thumb/bar/100x100bb.jpg",
                    }
                ]
            },
            status=200,
        )

        url = fetch_apple_cover("Test Song", "Test Artist")
        assert url is not None
        assert "1400x1400" in url

    @resp_lib.activate
    def test_fetch_apple_cover_no_results(self):
        from rubetunes.providers.apple_music import fetch_apple_cover

        resp_lib.add(
            resp_lib.GET,
            "https://itunes.apple.com/search",
            json={"results": []},
            status=200,
        )

        url = fetch_apple_cover("Nonexistent Song", "Nobody")
        assert url is None


# ===========================================================================
# SoundCloud / Bandcamp URL parsers (C1, C2)
# ===========================================================================

class TestSoundCloudParser:
    def test_valid_url(self):
        from rubetunes.providers.soundcloud import parse_soundcloud_url

        url = "https://soundcloud.com/artist-name/track-name"
        result = parse_soundcloud_url(url)
        assert result == url

    def test_invalid_url(self):
        from rubetunes.providers.soundcloud import parse_soundcloud_url

        assert parse_soundcloud_url("https://spotify.com/track/abc") is None

    def test_url_in_text(self):
        from rubetunes.providers.soundcloud import parse_soundcloud_url

        text = "check this out https://soundcloud.com/artist/song cool right"
        result = parse_soundcloud_url(text)
        assert result == "https://soundcloud.com/artist/song"


class TestBandcampParser:
    def test_valid_track_url(self):
        from rubetunes.providers.bandcamp import parse_bandcamp_url

        url = "https://artist.bandcamp.com/track/song-name"
        result = parse_bandcamp_url(url)
        assert result == url

    def test_valid_album_url(self):
        from rubetunes.providers.bandcamp import parse_bandcamp_url

        url = "https://artist.bandcamp.com/album/album-name"
        result = parse_bandcamp_url(url)
        assert result == url

    def test_invalid_url(self):
        from rubetunes.providers.bandcamp import parse_bandcamp_url

        assert parse_bandcamp_url("https://soundcloud.com/artist/song") is None
