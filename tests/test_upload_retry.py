"""Tests for rubetunes/upload_retry.py.

Tests the retry queue: enqueueing, persistence, retry tick logic,
cancellation, and the !uploads status helper.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rubetunes import upload_retry as ur

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_queue(tmp_path, monkeypatch):
    """Clear the in-memory queue and redirect queue/retry files to tmp_path."""
    ur._queue.clear()
    monkeypatch.setattr(ur, "_QUEUE_FILE", tmp_path / "upload_retry_queue.json")
    monkeypatch.setattr(ur, "UPLOAD_RETRY_DIR", tmp_path / ".upload_retry")
    monkeypatch.setattr(ur, "UPLOAD_RETRY_MAX_ATTEMPTS", 168)
    yield
    ur._queue.clear()


def _make_app(**overrides):
    """Return a mock app with async send_document and send_message."""
    app = MagicMock()
    app.send_document = AsyncMock(**overrides)
    app.send_message = AsyncMock()
    return app


# ---------------------------------------------------------------------------
# enqueue_failed_upload
# ---------------------------------------------------------------------------


class TestEnqueueFailedUpload:
    def test_moves_file_to_retry_dir(self, tmp_path):
        src = tmp_path / "song.mp3"
        src.write_bytes(b"audio data")

        app = _make_app()
        entry_id = ur.enqueue_failed_upload(
            app_send_document=app.send_document,
            object_guid="guid123",
            file_path=src,
            file_name="song.mp3",
            caption="My Song",
            provider="musicdl",
            exc=RuntimeError("net error"),
        )

        assert not src.exists(), "Original file should be moved"
        retry_file = ur.UPLOAD_RETRY_DIR / entry_id / "song.mp3"
        assert retry_file.exists(), "File should be in retry dir"

    def test_appends_queue_entry(self, tmp_path):
        src = tmp_path / "track.flac"
        src.write_bytes(b"flac data")

        app = _make_app()
        entry_id = ur.enqueue_failed_upload(
            app_send_document=app.send_document,
            object_guid="guid_abc",
            file_path=src,
            file_name="track.flac",
            caption="Some Caption",
            provider="spotify_track",
            exc=Exception("timeout"),
        )

        assert len(ur._queue) == 1
        entry = ur._queue[0]
        assert entry["id"] == entry_id
        assert entry["object_guid"] == "guid_abc"
        assert entry["file_name"] == "track.flac"
        assert entry["caption"] == "Some Caption"
        assert entry["provider"] == "spotify_track"
        assert entry["attempts"] == 1
        assert "timeout" in entry["last_error"]

    def test_persists_queue_to_disk(self, tmp_path):
        src = tmp_path / "a.mp3"
        src.write_bytes(b"data")
        app = _make_app()

        ur.enqueue_failed_upload(
            app_send_document=app.send_document,
            object_guid="g1",
            file_path=src,
            file_name="a.mp3",
            caption="",
            provider="youtube",
            exc=RuntimeError("err"),
        )

        assert ur._QUEUE_FILE.exists()
        data = json.loads(ur._QUEUE_FILE.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["provider"] == "youtube"


# ---------------------------------------------------------------------------
# load_on_startup
# ---------------------------------------------------------------------------


class TestLoadOnStartup:
    def test_loads_existing_entries(self, tmp_path):
        # Create a real file in the expected retry dir location
        entry_id = "aaaa-1234"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        queue_data = [
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "",
                "provider": "musicdl",
                "created_at": "2024-01-01T00:00:00+00:00",
                "attempts": 2,
                "last_attempt_at": "2024-01-01T01:00:00+00:00",
                "last_error": "net error",
            }
        ]
        ur._QUEUE_FILE.write_text(json.dumps(queue_data))
        ur.load_on_startup()

        assert len(ur._queue) == 1
        assert ur._queue[0]["id"] == entry_id

    def test_prunes_entries_with_missing_files(self, tmp_path):
        queue_data = [
            {
                "id": "bbbb-9999",
                "object_guid": "g1",
                "file_path": str(tmp_path / "nonexistent.mp3"),
                "file_name": "nonexistent.mp3",
                "caption": "",
                "provider": "youtube",
                "created_at": "2024-01-01T00:00:00+00:00",
                "attempts": 1,
                "last_attempt_at": "2024-01-01T00:00:00+00:00",
                "last_error": "err",
            }
        ]
        ur._QUEUE_FILE.write_text(json.dumps(queue_data))
        ur.load_on_startup()

        assert len(ur._queue) == 0, "Entries with missing files should be pruned"

    def test_handles_missing_queue_file(self):
        # Should not raise; queue stays empty
        ur.load_on_startup()
        assert ur._queue == []


# ---------------------------------------------------------------------------
# run_retry_tick — success path
# ---------------------------------------------------------------------------


class TestRetryTickSuccess:
    def test_removes_entry_on_success(self, tmp_path):
        entry_id = "succ-1"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "Cap",
                "provider": "musicdl",
                "created_at": "t",
                "attempts": 1,
                "last_attempt_at": "t",
                "last_error": "err",
            }
        )

        app = _make_app()  # send_document succeeds by default
        asyncio.run(ur.run_retry_tick(app))

        assert len(ur._queue) == 0, "Entry should be removed after success"

    def test_deletes_file_on_success(self, tmp_path):
        entry_id = "succ-2"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "track.flac"
        fp.write_bytes(b"flac")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g2",
                "file_path": str(fp),
                "file_name": "track.flac",
                "caption": "",
                "provider": "spotify_track",
                "created_at": "t",
                "attempts": 3,
                "last_attempt_at": "t",
                "last_error": "old err",
            }
        )

        app = _make_app()
        asyncio.run(ur.run_retry_tick(app))

        assert not fp.exists(), "Retry file should be deleted on success"

    def test_notifies_user_on_success(self, tmp_path):
        entry_id = "succ-3"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g3",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "",
                "provider": "youtube",
                "created_at": "t",
                "attempts": 2,
                "last_attempt_at": "t",
                "last_error": "err",
            }
        )

        app = _make_app()
        asyncio.run(ur.run_retry_tick(app))

        app.send_message.assert_called_once()
        msg = app.send_message.call_args[0][1]
        assert "✅" in msg
        assert "song.mp3" in msg


# ---------------------------------------------------------------------------
# run_retry_tick — failure paths
# ---------------------------------------------------------------------------


class TestRetryTickFailure:
    def test_increments_attempts_on_failure(self, tmp_path):
        entry_id = "fail-1"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "",
                "provider": "musicdl",
                "created_at": "t",
                "attempts": 1,
                "last_attempt_at": "t",
                "last_error": "err",
            }
        )

        app = _make_app(side_effect=RuntimeError("still failing"))
        asyncio.run(ur.run_retry_tick(app))

        assert len(ur._queue) == 1, "Failed entry should remain in queue"
        assert ur._queue[0]["attempts"] == 2

    def test_gives_up_after_max_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ur, "UPLOAD_RETRY_MAX_ATTEMPTS", 3)

        entry_id = "fail-max"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "",
                "provider": "musicdl",
                "created_at": "t",
                "attempts": 3,  # at max
                "last_attempt_at": "t",
                "last_error": "prev err",
            }
        )

        app = _make_app(side_effect=RuntimeError("still failing"))
        asyncio.run(ur.run_retry_tick(app))

        assert len(ur._queue) == 0, "Entry should be dropped after max attempts"
        assert not fp.exists(), "File should be deleted when giving up"
        # User gets a final failure message
        app.send_message.assert_called_once()
        msg = app.send_message.call_args[0][1]
        assert "❌" in msg
        assert "song.mp3" in msg

    def test_drops_entry_if_file_gone(self, tmp_path):
        """If the file disappears between ticks, the entry is dropped silently."""
        entry_id = "gone-1"

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(tmp_path / "nonexistent.mp3"),
                "file_name": "nonexistent.mp3",
                "caption": "",
                "provider": "youtube",
                "created_at": "t",
                "attempts": 1,
                "last_attempt_at": "t",
                "last_error": "err",
            }
        )

        app = _make_app()
        asyncio.run(ur.run_retry_tick(app))

        assert len(ur._queue) == 0, "Entry with missing file should be dropped"


# ---------------------------------------------------------------------------
# cancel_entry
# ---------------------------------------------------------------------------


class TestCancelEntry:
    def test_cancel_removes_entry(self, tmp_path):
        entry_id = "canc-1"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        ur._queue.append(
            {
                "id": entry_id,
                "object_guid": "g1",
                "file_path": str(fp),
                "file_name": "song.mp3",
                "caption": "",
                "provider": "musicdl",
                "created_at": "t",
                "attempts": 1,
                "last_attempt_at": "t",
                "last_error": "err",
            }
        )

        result = ur.cancel_entry(entry_id)

        assert result is True
        assert len(ur._queue) == 0
        assert not fp.exists(), "Cancelled file should be deleted"

    def test_cancel_returns_false_for_unknown_id(self):
        result = ur.cancel_entry("nonexistent-id")
        assert result is False


# ---------------------------------------------------------------------------
# list_entries
# ---------------------------------------------------------------------------


class TestListEntries:
    def test_returns_copy_of_queue(self, tmp_path):
        ur._queue.append({"id": "x", "file_name": "a.mp3"})
        entries = ur.list_entries()
        assert entries == [{"id": "x", "file_name": "a.mp3"}]
        # Mutating the returned list does not affect the internal queue
        entries.pop()
        assert len(ur._queue) == 1


# ---------------------------------------------------------------------------
# Persistence: save and reload
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_queue_survives_reload(self, tmp_path):
        """Saving and reloading the queue preserves all entries."""
        entry_id = "pers-1"
        dest_dir = ur.UPLOAD_RETRY_DIR / entry_id
        dest_dir.mkdir(parents=True)
        fp = dest_dir / "song.mp3"
        fp.write_bytes(b"audio")

        app = _make_app()
        ur.enqueue_failed_upload(
            app_send_document=app.send_document,
            object_guid="g1",
            file_path=tmp_path / "source.mp3",
            file_name="song.mp3",
            caption="Cap",
            provider="youtube",
            exc=RuntimeError("err"),
        )

        # Simulate restart: clear memory, reload from disk
        saved_path = ur._QUEUE_FILE
        ur._queue.clear()
        # Patch the queue file back (already done by fixture)
        # Re-populate a real file so load_on_startup doesn't prune it
        entry = json.loads(saved_path.read_text())[0]
        fp2 = Path(entry["file_path"])
        fp2.parent.mkdir(parents=True, exist_ok=True)
        fp2.write_bytes(b"audio")

        ur.load_on_startup()

        assert len(ur._queue) == 1
        assert ur._queue[0]["provider"] == "youtube"
