"""
Tests for the musicdl provider integration.

All tests that require the musicdl package are marked with
``pytest.mark.skipif(not _HAS_MUSICDL, ...)`` so CI stays green even when
musicdl is not installed in the test runner environment.

The search and download paths are fully mocked — no real network calls.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Detect musicdl availability (import-time check)
# ---------------------------------------------------------------------------
try:
    import musicdl  # type: ignore[import]  # noqa: F401

    _HAS_MUSICDL = True
except ImportError:
    _HAS_MUSICDL = False

_SKIP_NO_MUSICDL = pytest.mark.skipif(
    not _HAS_MUSICDL, reason="musicdl package is not installed"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_song_info(**kwargs):
    """Return a minimal object that quacks like musicdl's SongInfo."""
    defaults = {
        "song_name": "Test Song",
        "singers": "Test Artist",
        "album": "Test Album",
        "source": "NeteaseMusicClient",
        "file_size": "5.0 MB",
        "duration": "3:30",
        "song_id": "12345",
        "ext": "flac",
        "cover_url": "https://example.com/cover.jpg",
        "lyric": "",
        "file_path": "/tmp/test_song.flac",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ===========================================================================
# 1. Errors module — importable without musicdl
# ===========================================================================


class TestErrors:
    def test_import_errors_module(self):
        from rubetunes.providers.musicdl.errors import (
            MusicdlDownloadError,
            MusicdlError,
            MusicdlNotInstalledError,
            MusicdlSearchError,
        )

        assert issubclass(MusicdlNotInstalledError, MusicdlError)
        assert issubclass(MusicdlSearchError, MusicdlError)
        assert issubclass(MusicdlDownloadError, MusicdlError)

    def test_not_installed_error_message(self):
        from rubetunes.providers.musicdl.errors import MusicdlNotInstalledError

        exc = MusicdlNotInstalledError()
        assert "musicdl" in str(exc).lower()
        assert "pip install" in str(exc)


# ===========================================================================
# 2. Config module — importable without musicdl
# ===========================================================================


class TestConfig:
    def test_default_download_dir(self):
        from rubetunes.providers.musicdl.config import MUSICDL_DOWNLOAD_DIR

        assert isinstance(MUSICDL_DOWNLOAD_DIR, Path)

    def test_build_init_cfg_returns_work_dir(self, tmp_path, monkeypatch):
        import rubetunes.providers.musicdl.config as cfg_module

        monkeypatch.setattr(cfg_module, "MUSICDL_DOWNLOAD_DIR", tmp_path)
        result = cfg_module.build_init_cfg("NeteaseMusicClient")
        assert "work_dir" in result
        assert result["disable_print"] is True

    def test_build_requests_overrides_no_proxy(self, monkeypatch):
        import rubetunes.providers.musicdl.config as cfg_module

        monkeypatch.setattr(cfg_module, "MUSICDL_PROXY", None)
        assert cfg_module.build_requests_overrides() == {}

    def test_build_requests_overrides_with_proxy(self, monkeypatch):
        import rubetunes.providers.musicdl.config as cfg_module

        monkeypatch.setattr(cfg_module, "MUSICDL_PROXY", "http://proxy:8080")
        overrides = cfg_module.build_requests_overrides()
        assert "proxies" in overrides
        assert overrides["proxies"]["http"] == "http://proxy:8080"


# ===========================================================================
# 3. Models module — importable without musicdl
# ===========================================================================


class TestModels:
    def test_musicdl_track_from_song_info(self):
        from rubetunes.providers.musicdl.models import MusicdlTrack

        raw = _make_song_info()
        track = MusicdlTrack.from_song_info(raw)
        assert track.song_name == "Test Song"
        assert track.singers == "Test Artist"
        assert track.source == "NeteaseMusicClient"
        assert track._raw is raw

    def test_display_title_with_singers(self):
        from rubetunes.providers.musicdl.models import MusicdlTrack

        track = MusicdlTrack(song_name="Song", singers="Artist")
        assert track.display_title == "Artist — Song"

    def test_display_title_no_singers(self):
        from rubetunes.providers.musicdl.models import MusicdlTrack

        track = MusicdlTrack(song_name="Song")
        assert track.display_title == "Song"

    def test_search_result_defaults(self):
        from rubetunes.providers.musicdl.models import MusicdlSearchResult

        result = MusicdlSearchResult(query="test")
        assert result.tracks == []
        assert result.total == 0

    def test_download_result_defaults(self):
        from rubetunes.providers.musicdl.models import MusicdlDownloadResult

        result = MusicdlDownloadResult()
        assert result.success is False


# ===========================================================================
# 4. Client.list_sources — needs musicdl installed
# ===========================================================================


@_SKIP_NO_MUSICDL
class TestListSources:
    def test_list_sources_returns_nonempty(self):
        from rubetunes.providers.musicdl.client import MusicdlClient

        client = MusicdlClient()
        sources = client.list_sources()
        assert isinstance(sources, list)
        assert len(sources) > 0

    def test_list_sources_contains_known_clients(self):
        from rubetunes.providers.musicdl.client import MusicdlClient

        client = MusicdlClient()
        sources = client.list_sources()
        # At least one Chinese platform should always be present
        assert any("MusicClient" in s for s in sources)

    def test_list_sources_is_sorted(self):
        from rubetunes.providers.musicdl.client import MusicdlClient

        client = MusicdlClient()
        sources = client.list_sources()
        assert sources == sorted(sources)


# ===========================================================================
# 5. Client.search — fully mocked (no network)
# ===========================================================================


class TestMusicdlSearch:
    def _make_client_with_mock_musicdl(self, search_return: dict):
        """Patch _import_musicdl and _import_client_builder for testing."""

        mock_mc_instance = MagicMock()
        mock_mc_instance.search.return_value = search_return
        mock_mc_class = MagicMock(return_value=mock_mc_instance)

        return mock_mc_class, mock_mc_instance

    @pytest.mark.asyncio
    async def test_search_returns_tracks(self):
        from rubetunes.providers.musicdl import client as client_mod
        from rubetunes.providers.musicdl.client import MusicdlClient

        raw_info = _make_song_info()
        mock_class, mock_instance = self._make_client_with_mock_musicdl(
            {"NeteaseMusicClient": [raw_info]}
        )

        with patch.object(client_mod, "_import_musicdl", return_value=mock_class):
            c = MusicdlClient(sources=["NeteaseMusicClient"])
            result = await c.search("Test Song")

        assert result.total == 1
        assert result.tracks[0].song_name == "Test Song"
        mock_instance.search.assert_called_once_with(keyword="Test Song")

    @pytest.mark.asyncio
    async def test_search_empty_query_raises(self):
        from rubetunes.providers.musicdl.client import MusicdlClient
        from rubetunes.providers.musicdl.errors import MusicdlSearchError

        c = MusicdlClient()
        with pytest.raises(MusicdlSearchError, match="empty"):
            await c.search("")

    @pytest.mark.asyncio
    async def test_search_not_installed_raises(self):
        from rubetunes.providers.musicdl import client as client_mod
        from rubetunes.providers.musicdl.client import MusicdlClient
        from rubetunes.providers.musicdl.errors import MusicdlNotInstalledError

        with patch.object(client_mod, "_import_musicdl", side_effect=MusicdlNotInstalledError):
            c = MusicdlClient(sources=["NeteaseMusicClient"])
            with pytest.raises(MusicdlNotInstalledError):
                await c.search("hello")

    @pytest.mark.asyncio
    async def test_search_multiple_sources_merged(self):
        from rubetunes.providers.musicdl import client as client_mod
        from rubetunes.providers.musicdl.client import MusicdlClient

        netease_track = _make_song_info(source="NeteaseMusicClient", song_name="Song A")
        qq_track = _make_song_info(source="QQMusicClient", song_name="Song B")

        mock_class, mock_instance = self._make_client_with_mock_musicdl(
            {"NeteaseMusicClient": [netease_track], "QQMusicClient": [qq_track]}
        )

        with patch.object(client_mod, "_import_musicdl", return_value=mock_class):
            c = MusicdlClient(sources=["NeteaseMusicClient", "QQMusicClient"])
            result = await c.search("query")

        assert result.total == 2
        assert len(result.by_source["NeteaseMusicClient"]) == 1
        assert len(result.by_source["QQMusicClient"]) == 1


# ===========================================================================
# 6. Client.download — fully mocked (no network)
# ===========================================================================


class TestMusicdlDownload:
    @pytest.mark.asyncio
    async def test_download_success(self, tmp_path):
        from rubetunes.providers.musicdl import client as client_mod
        from rubetunes.providers.musicdl.client import MusicdlClient
        from rubetunes.providers.musicdl.models import MusicdlTrack

        raw = _make_song_info(file_path=str(tmp_path / "song.flac"))
        track = MusicdlTrack.from_song_info(raw)

        downloaded_info = _make_song_info(file_path=str(tmp_path / "song.flac"))
        mock_instance = MagicMock()
        mock_instance.download.return_value = [downloaded_info]
        mock_class = MagicMock(return_value=mock_instance)

        with patch.object(client_mod, "_import_musicdl", return_value=mock_class):
            c = MusicdlClient(sources=["NeteaseMusicClient"])
            # Create the file so exists() returns True
            (tmp_path / "song.flac").write_bytes(b"fake audio")
            result = await c.download(track, dest_dir=tmp_path)

        assert result.success is True
        assert result.file_path == tmp_path / "song.flac"
        mock_instance.download.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_no_raw_raises(self, tmp_path):
        from rubetunes.providers.musicdl.client import MusicdlClient
        from rubetunes.providers.musicdl.errors import MusicdlDownloadError
        from rubetunes.providers.musicdl.models import MusicdlTrack

        track = MusicdlTrack(song_name="Song", source="NeteaseMusicClient")
        c = MusicdlClient()
        with pytest.raises(MusicdlDownloadError, match="raw SongInfo"):
            await c.download(track, dest_dir=tmp_path)


# ===========================================================================
# 7. Public __init__ exports
# ===========================================================================


class TestPublicExports:
    def test_all_exports_importable(self):
        import rubetunes.providers.musicdl as mdl

        for name in mdl.__all__:
            assert hasattr(mdl, name), f"Missing export: {name}"
