"""Tests for _musicdl_pick file-cleanup behaviour.

After ``!musicdl <n>`` the bot must delete the downloaded file from disk,
regardless of whether the upload succeeded or failed.  Empty per-source
sub-directories should also be removed; MUSICDL_DOWNLOAD_DIR itself must
never be deleted.

rub.py cannot be imported directly in a normal test environment because it
depends on ``rubpy`` (the Rubika client library).  We inject a lightweight
fake into ``sys.modules`` before loading the module with ``importlib`` so
that we can exercise ``_musicdl_pick`` end-to-end with everything else
mocked.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers for injecting a fake ``rubpy`` so rub.py can be imported
# ---------------------------------------------------------------------------

def _make_rubpy_mock() -> types.ModuleType:
    """Return a minimal fake ``rubpy`` module accepted by rub.py."""
    mod = types.ModuleType("rubpy")
    # filters.commands is used as a decorator factory — make it a passthrough
    filters_mock = MagicMock()
    filters_mock.commands = lambda *a, **kw: lambda f: f
    mod.filters = filters_mock  # type: ignore[attr-defined]
    # Client() is called at module level to create ``app``
    mod.Client = MagicMock  # type: ignore[attr-defined]
    return mod


def _load_rub() -> types.ModuleType:
    """Import ``rub`` with a fake ``rubpy`` injected, returning the module."""
    if "rubpy" not in sys.modules:
        sys.modules["rubpy"] = _make_rubpy_mock()
    if "rub" in sys.modules:
        return sys.modules["rub"]
    return importlib.import_module("rub")


# Load rub once at collection time — all tests in this file share the import.
_rub = _load_rub()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_app(monkeypatch):
    """Replace the module-level ``app`` object with async-friendly mocks."""
    app = MagicMock()
    app.send_message = AsyncMock(return_value=MagicMock(message_id="status-1"))
    app.edit_message = AsyncMock()
    app.send_document = AsyncMock()
    app.delete_messages = AsyncMock()
    monkeypatch.setattr(_rub, "app", app)
    return app


@pytest.fixture()
def mock_musicdl_client(monkeypatch):
    """Return a factory that configures MusicdlClient.download for a given file."""
    from rubetunes.providers.musicdl.models import MusicdlDownloadResult, MusicdlTrack

    def _configure(file_path: Path, success: bool = True, error: str = ""):
        dl_result = MusicdlDownloadResult(
            track=MusicdlTrack(song_name="Test Song", singers="Artist", source="FakeMusicClient"),
            file_path=file_path,
            success=success,
            error=error,
        )
        client_instance = MagicMock()
        client_instance.download = AsyncMock(return_value=dl_result)
        client_cls = MagicMock(return_value=client_instance)
        monkeypatch.setattr(_rub, "MusicdlClient", client_cls)
        monkeypatch.setattr(_rub, "_HAS_MUSICDL", True)
        return client_instance

    return _configure


@pytest.fixture()
def pending_selection(monkeypatch):
    """Populate _musicdl_selections with a single fake pending search result."""
    from rubetunes.providers.musicdl.models import MusicdlTrack

    guid = "test_guid"
    track = MusicdlTrack(
        song_name="Test Song",
        singers="Artist",
        source="FakeMusicClient",
        _raw=MagicMock(),
    )
    monkeypatch.setitem(
        _rub._musicdl_selections,
        guid,
        {"tracks": [track], "timeout_task": None},
    )
    return guid, track


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestMusicdlPickCleanup:
    """_musicdl_pick must always delete the downloaded file after upload."""

    def test_file_deleted_after_successful_upload(
        self, tmp_path, mock_app, mock_musicdl_client, pending_selection, monkeypatch
    ):
        """Successful upload: downloaded file is removed from disk."""
        guid, _ = pending_selection

        # Create a real temp file that represents the downloaded track
        source_dir = tmp_path / "FakeMusicClient"
        source_dir.mkdir()
        audio_file = source_dir / "test_song.flac"
        audio_file.write_bytes(b"fake audio data")

        mock_musicdl_client(audio_file, success=True)
        monkeypatch.setattr(_rub, "_HAS_RATE_LIMITER", False)

        log = logging.getLogger("test_musicdl_pick")
        asyncio.run(_rub._musicdl_pick(guid, 1, log))

        assert not audio_file.exists(), "Downloaded file should be deleted after successful upload"

    def test_file_deleted_after_failed_upload(
        self, tmp_path, mock_app, mock_musicdl_client, pending_selection, monkeypatch
    ):
        """Upload failure: downloaded file is still removed from disk (no orphans)."""
        guid, _ = pending_selection

        source_dir = tmp_path / "FakeMusicClient"
        source_dir.mkdir()
        audio_file = source_dir / "test_song.mp3"
        audio_file.write_bytes(b"fake audio data")

        mock_musicdl_client(audio_file, success=True)
        monkeypatch.setattr(_rub, "_HAS_RATE_LIMITER", False)

        # Make send_document raise to simulate an upload failure
        mock_app.send_document.side_effect = RuntimeError("network error")

        log = logging.getLogger("test_musicdl_pick")
        asyncio.run(_rub._musicdl_pick(guid, 1, log))

        assert not audio_file.exists(), "Downloaded file should be deleted even when upload fails"

    def test_empty_source_dir_removed(
        self, tmp_path, mock_app, mock_musicdl_client, pending_selection, monkeypatch
    ):
        """After file deletion, an empty per-source subdirectory is cleaned up."""
        guid, _ = pending_selection

        source_dir = tmp_path / "FakeMusicClient"
        source_dir.mkdir()
        audio_file = source_dir / "test_song.flac"
        audio_file.write_bytes(b"fake audio data")

        mock_musicdl_client(audio_file, success=True)
        monkeypatch.setattr(_rub, "_HAS_RATE_LIMITER", False)

        import rubetunes.providers.musicdl.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "MUSICDL_DOWNLOAD_DIR", tmp_path)

        log = logging.getLogger("test_musicdl_pick")
        asyncio.run(_rub._musicdl_pick(guid, 1, log))

        assert not source_dir.exists(), "Empty per-source dir should be removed"
        assert tmp_path.exists(), "MUSICDL_DOWNLOAD_DIR itself must never be deleted"

    def test_musicdl_download_dir_never_deleted(
        self, tmp_path, mock_app, mock_musicdl_client, pending_selection, monkeypatch
    ):
        """MUSICDL_DOWNLOAD_DIR itself is always preserved."""
        guid, _ = pending_selection

        audio_file = tmp_path / "test_song.flac"
        audio_file.write_bytes(b"fake audio data")

        mock_musicdl_client(audio_file, success=True)
        monkeypatch.setattr(_rub, "_HAS_RATE_LIMITER", False)

        import rubetunes.providers.musicdl.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "MUSICDL_DOWNLOAD_DIR", tmp_path)

        log = logging.getLogger("test_musicdl_pick")
        asyncio.run(_rub._musicdl_pick(guid, 1, log))

        assert not audio_file.exists(), "File should be deleted"
        assert tmp_path.exists(), "MUSICDL_DOWNLOAD_DIR must not be deleted"

    def test_nonempty_source_dir_preserved(
        self, tmp_path, mock_app, mock_musicdl_client, pending_selection, monkeypatch
    ):
        """A source directory that still has files in it must not be removed."""
        guid, _ = pending_selection

        source_dir = tmp_path / "FakeMusicClient"
        source_dir.mkdir()
        audio_file = source_dir / "test_song.flac"
        audio_file.write_bytes(b"fake audio data")
        # Leave another file behind so the directory is not empty after cleanup
        (source_dir / "other.flac").write_bytes(b"other data")

        mock_musicdl_client(audio_file, success=True)
        monkeypatch.setattr(_rub, "_HAS_RATE_LIMITER", False)

        import rubetunes.providers.musicdl.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "MUSICDL_DOWNLOAD_DIR", tmp_path)

        log = logging.getLogger("test_musicdl_pick")
        asyncio.run(_rub._musicdl_pick(guid, 1, log))

        assert not audio_file.exists(), "Downloaded file should be deleted"
        assert source_dir.exists(), "Non-empty source dir must be preserved"
        assert (source_dir / "other.flac").exists(), "Unrelated files must not be touched"
