"""Tests for rubetunes/yt_notify.py — YouTube channel subscription notifier."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import rubetunes.yt_notify as yn

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    """Isolate state between tests: redirect the state file and reset in-memory state."""
    state_file = tmp_path / "ytsub_state.json"
    monkeypatch.setattr(yn, "_STATE_FILE", state_file)
    monkeypatch.setattr(yn, "_state", {"channels": {}})
    yield
    # Cleanup
    monkeypatch.setattr(yn, "_state", {"channels": {}})


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


class TestNormaliseInputUrl:
    @pytest.mark.parametrize(
        "raw,expected_suffix",
        [
            (
                "https://www.youtube.com/@MyCoolChannel",
                "/@MyCoolChannel/videos",
            ),
            (
                "https://www.youtube.com/@MyCoolChannel/videos",
                "/@MyCoolChannel/videos",
            ),
            (
                "https://www.youtube.com/@MyCoolChannel/about",
                "/@MyCoolChannel/videos",
            ),
            (
                "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx",
                "/channel/UCxxxxxxxxxxxxxxxxxxxxxx/videos",
            ),
            (
                "https://www.youtube.com/c/SomeLegacyName",
                "/c/SomeLegacyName/videos",
            ),
            (
                "https://www.youtube.com/user/SomeLegacyUser",
                "/user/SomeLegacyUser/videos",
            ),
        ],
    )
    def test_valid_urls(self, raw, expected_suffix):
        result = yn._normalise_input_url(raw)
        assert result is not None
        assert result.endswith(expected_suffix), f"{result!r} should end with {expected_suffix!r}"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://open.spotify.com/track/abc",
            "not a url at all",
        ],
    )
    def test_invalid_urls_return_none(self, raw):
        assert yn._normalise_input_url(raw) is None


# ---------------------------------------------------------------------------
# resolve_channel (mocked yt-dlp)
# ---------------------------------------------------------------------------

_FAKE_CHANNEL_JSON = json.dumps(
    {
        "channel_id": "UCfake123456789012345678",
        "channel": "Fake Channel",
        "uploader": "Fake Channel",
        "webpage_url": "https://www.youtube.com/channel/UCfake123456789012345678",
        "entries": [],
    }
)


class TestResolveChannel:
    def test_success(self):
        with patch.object(yn, "_run_ytdlp", return_value=(0, _FAKE_CHANNEL_JSON, "")) as mock_run:
            result = yn.resolve_channel("https://www.youtube.com/@FakeChannel/videos")

        assert result is not None
        assert result["channel_id"] == "UCfake123456789012345678"
        assert result["channel_name"] == "Fake Channel"
        assert result["channel_url"] == "https://www.youtube.com/channel/UCfake123456789012345678/videos"
        mock_run.assert_called_once()

    def test_nonzero_returncode_returns_none(self):
        with patch.object(yn, "_run_ytdlp", return_value=(1, "", "some error")):
            result = yn.resolve_channel("https://www.youtube.com/@Bad/videos")
        assert result is None

    def test_invalid_json_returns_none(self):
        with patch.object(yn, "_run_ytdlp", return_value=(0, "not-json", "")):
            result = yn.resolve_channel("https://www.youtube.com/@Bad/videos")
        assert result is None

    def test_missing_channel_id_returns_none(self):
        payload = json.dumps({"channel": "No ID Channel"})
        with patch.object(yn, "_run_ytdlp", return_value=(0, payload, "")):
            result = yn.resolve_channel("https://www.youtube.com/@Bad/videos")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_latest_video_ids (mocked yt-dlp)
# ---------------------------------------------------------------------------

_FAKE_PLAYLIST_JSON = json.dumps(
    {
        "channel_id": "UCfake123456789012345678",
        "entries": [
            {
                "id": "vid_001",
                "title": "First Video",
                "url": "https://www.youtube.com/watch?v=vid_001",
            },
            {
                "id": "vid_002",
                "title": "Second Video",
                "url": "https://www.youtube.com/watch?v=vid_002",
            },
        ],
    }
)


class TestFetchLatestVideoIds:
    def test_success(self):
        with patch.object(yn, "_run_ytdlp", return_value=(0, _FAKE_PLAYLIST_JSON, "")):
            videos = yn.fetch_latest_video_ids(
                "https://www.youtube.com/channel/UCfake123456789012345678/videos", count=2
            )

        assert len(videos) == 2
        assert videos[0]["id"] == "vid_001"
        assert videos[0]["title"] == "First Video"
        assert videos[1]["id"] == "vid_002"

    def test_failure_returns_empty(self):
        with patch.object(yn, "_run_ytdlp", return_value=(1, "", "error")):
            videos = yn.fetch_latest_video_ids("https://www.youtube.com/@x/videos")
        assert videos == []

    def test_invalid_json_returns_empty(self):
        with patch.object(yn, "_run_ytdlp", return_value=(0, "bad-json", "")):
            videos = yn.fetch_latest_video_ids("https://www.youtube.com/@x/videos")
        assert videos == []


# ---------------------------------------------------------------------------
# subscribe_sync
# ---------------------------------------------------------------------------


def _make_resolve_and_fetch(
    channel_id="UCabc12345678901234567890",
    channel_name="Test Channel",
    video_ids=None,
):
    """Return side_effect callables for _run_ytdlp that simulate resolve + fetch."""
    video_ids = video_ids or [f"v{i:03d}" for i in range(10)]
    entries = [
        {"id": vid, "title": f"Video {vid}", "url": f"https://www.youtube.com/watch?v={vid}"}
        for vid in video_ids
    ]
    channel_json = json.dumps(
        {
            "channel_id": channel_id,
            "channel": channel_name,
            "entries": entries[:1],
        }
    )
    playlist_json = json.dumps({"entries": entries})

    call_count = 0

    def _side_effect(args):
        nonlocal call_count
        call_count += 1
        # First call → resolve; subsequent → playlist fetch.
        if call_count == 1:
            return (0, channel_json, "")
        return (0, playlist_json, "")

    return _side_effect


class TestSubscribeSync:
    def test_invalid_url(self):
        ok, msg, record, seeds = yn.subscribe_sync("not-a-url", "user1")
        assert not ok
        assert "Invalid" in msg

    def test_first_subscriber_seeds_videos(self):
        side_effect = _make_resolve_and_fetch(video_ids=["v001", "v002", "v003"])
        with patch.object(yn, "_run_ytdlp", side_effect=side_effect):
            ok, msg, record, seeds = yn.subscribe_sync(
                "https://www.youtube.com/@TestChannel", "user1"
            )

        assert ok, msg
        assert record is not None
        assert "user1" in record["subscribers"]
        assert len(seeds) > 0
        assert {v["id"] for v in seeds} == {"v001", "v002", "v003"}

    def test_last_seen_ids_populated_on_first_sub(self):
        side_effect = _make_resolve_and_fetch(video_ids=["v001", "v002"])
        with patch.object(yn, "_run_ytdlp", side_effect=side_effect):
            yn.subscribe_sync("https://www.youtube.com/@TestChannel", "user1")

        channel_id = list(yn._state["channels"].keys())[0]
        assert set(yn._state["channels"][channel_id]["last_seen_video_ids"]) == {"v001", "v002"}

    def test_second_subscriber_no_seed(self):
        side_effect = _make_resolve_and_fetch()
        with patch.object(yn, "_run_ytdlp", side_effect=side_effect):
            yn.subscribe_sync("https://www.youtube.com/@TestChannel", "user1")

        # Second subscription to the same channel: only resolve call, no fetch.
        channel_id = list(yn._state["channels"].keys())[0]

        resolve_json = json.dumps(
            {
                "channel_id": channel_id,
                "channel": "Test Channel",
                "entries": [],
            }
        )
        with patch.object(yn, "_run_ytdlp", return_value=(0, resolve_json, "")) as mock_run:
            ok, msg, record, seeds = yn.subscribe_sync(
                "https://www.youtube.com/@TestChannel", "user2"
            )

        assert ok, msg
        assert seeds == [], "Second subscriber should not receive seed videos"
        assert "user2" in record["subscribers"]
        assert "user1" in record["subscribers"]
        # Only one yt-dlp call (resolve) — no playlist fetch for existing channel.
        assert mock_run.call_count == 1

    def test_already_subscribed(self):
        side_effect = _make_resolve_and_fetch()
        with patch.object(yn, "_run_ytdlp", side_effect=side_effect):
            yn.subscribe_sync("https://www.youtube.com/@TestChannel", "user1")

        channel_id = list(yn._state["channels"].keys())[0]
        resolve_json = json.dumps(
            {
                "channel_id": channel_id,
                "channel": "Test Channel",
                "entries": [],
            }
        )
        with patch.object(yn, "_run_ytdlp", return_value=(0, resolve_json, "")):
            ok, msg, record, seeds = yn.subscribe_sync(
                "https://www.youtube.com/@TestChannel", "user1"
            )

        assert not ok
        assert "already subscribed" in msg.lower()

    def test_state_persisted_after_subscribe(self, tmp_path):
        side_effect = _make_resolve_and_fetch()
        with patch.object(yn, "_run_ytdlp", side_effect=side_effect):
            yn.subscribe_sync("https://www.youtube.com/@TestChannel", "user1")

        assert yn._STATE_FILE.exists()
        saved = json.loads(yn._STATE_FILE.read_text())
        assert len(saved["channels"]) == 1


# ---------------------------------------------------------------------------
# unsubscribe_sync
# ---------------------------------------------------------------------------


class TestUnsubscribeSync:
    def _seed_channel(self, channel_id="UCabc12345678901234567890", subscribers=None):
        """Directly insert a channel into _state without hitting yt-dlp."""
        yn._state["channels"][channel_id] = {
            "channel_id": channel_id,
            "channel_name": "Test Channel",
            "channel_url": f"https://www.youtube.com/channel/{channel_id}/videos",
            "subscribers": list(subscribers or ["user1"]),
            "last_seen_video_ids": ["v001", "v002"],
        }

    def test_unsubscribe_valid(self):
        channel_id = "UCabc123456789012345678A"
        self._seed_channel(channel_id, subscribers=["user1", "user2"])

        ok, msg = yn.unsubscribe_sync(
            f"https://www.youtube.com/channel/{channel_id}/videos", "user1"
        )

        assert ok
        assert "Unsubscribed" in msg
        # user1 removed, user2 still there, channel record kept.
        assert "user1" not in yn._state["channels"][channel_id]["subscribers"]
        assert "user2" in yn._state["channels"][channel_id]["subscribers"]

    def test_last_subscriber_removes_channel(self):
        channel_id = "UCabc123456789012345678A"
        self._seed_channel(channel_id, subscribers=["user1"])

        ok, msg = yn.unsubscribe_sync(
            f"https://www.youtube.com/channel/{channel_id}/videos", "user1"
        )

        assert ok
        assert channel_id not in yn._state["channels"]

    def test_not_subscribed(self):
        channel_id = "UCabc123456789012345678A"
        self._seed_channel(channel_id, subscribers=["user1"])

        ok, msg = yn.unsubscribe_sync(
            f"https://www.youtube.com/channel/{channel_id}/videos", "user_notexist"
        )

        assert not ok

    def test_invalid_url(self):
        ok, msg = yn.unsubscribe_sync("not-a-url", "user1")
        assert not ok
        assert "Invalid" in msg


# ---------------------------------------------------------------------------
# list_subscriptions_sync
# ---------------------------------------------------------------------------


class TestListSubscriptionsSync:
    def test_empty(self):
        result = yn.list_subscriptions_sync("user1")
        assert result == []

    def test_subscribed_channels_returned(self):
        yn._state["channels"]["UC1"] = {
            "channel_id": "UC1",
            "channel_name": "Channel 1",
            "channel_url": "https://www.youtube.com/channel/UC1/videos",
            "subscribers": ["user1", "user2"],
            "last_seen_video_ids": [],
        }
        yn._state["channels"]["UC2"] = {
            "channel_id": "UC2",
            "channel_name": "Channel 2",
            "channel_url": "https://www.youtube.com/channel/UC2/videos",
            "subscribers": ["user2"],
            "last_seen_video_ids": [],
        }

        user1_subs = yn.list_subscriptions_sync("user1")
        assert len(user1_subs) == 1
        assert user1_subs[0]["channel_id"] == "UC1"

        user2_subs = yn.list_subscriptions_sync("user2")
        assert len(user2_subs) == 2


# ---------------------------------------------------------------------------
# _poll_once — async
# ---------------------------------------------------------------------------


class TestPollOnce:
    def _seed_channel(
        self,
        channel_id="UCpoll000000000000000001",
        last_seen=None,
        subscribers=None,
    ):
        yn._state["channels"][channel_id] = {
            "channel_id": channel_id,
            "channel_name": "Poll Channel",
            "channel_url": f"https://www.youtube.com/channel/{channel_id}/videos",
            "subscribers": list(subscribers or ["userA"]),
            "last_seen_video_ids": list(last_seen or ["old_vid"]),
        }

    def test_new_video_notifies_subscribers(self):
        channel_id = "UCpoll000000000000000001"
        self._seed_channel(channel_id, last_seen=["old_vid"], subscribers=["userA", "userB"])

        new_playlist_json = json.dumps(
            {
                "entries": [
                    {
                        "id": "new_vid",
                        "title": "Brand New Video",
                        "url": "https://www.youtube.com/watch?v=new_vid",
                    },
                    {
                        "id": "old_vid",
                        "title": "Old Video",
                        "url": "https://www.youtube.com/watch?v=old_vid",
                    },
                ]
            }
        )

        sent_messages: list[tuple[str, str]] = []

        async def _send(guid, text):
            sent_messages.append((guid, text))

        with patch.object(yn, "_run_ytdlp", return_value=(0, new_playlist_json, "")):
            asyncio.run(yn._poll_once(_send))

        # Both subscribers notified.
        guids_notified = {m[0] for m in sent_messages}
        assert "userA" in guids_notified
        assert "userB" in guids_notified

        # Message content mentions the new video.
        for _, text in sent_messages:
            assert "Brand New Video" in text

        # last_seen updated.
        assert "new_vid" in yn._state["channels"][channel_id]["last_seen_video_ids"]

    def test_no_new_videos_no_notification(self):
        channel_id = "UCpoll000000000000000002"
        self._seed_channel(channel_id, last_seen=["vid001"], subscribers=["userA"])

        same_playlist_json = json.dumps(
            {
                "entries": [
                    {
                        "id": "vid001",
                        "title": "Already Seen",
                        "url": "https://www.youtube.com/watch?v=vid001",
                    }
                ]
            }
        )

        sent_messages: list = []

        async def _send(guid, text):
            sent_messages.append((guid, text))

        with patch.object(yn, "_run_ytdlp", return_value=(0, same_playlist_json, "")):
            asyncio.run(yn._poll_once(_send))

        assert sent_messages == [], "No messages should be sent when there are no new videos"

    def test_single_ytdlp_call_per_channel(self):
        """Per-channel poll constraint: 2 subscribers → 1 yt-dlp call, 2 DMs."""
        channel_id = "UCpoll000000000000000003"
        self._seed_channel(channel_id, last_seen=[], subscribers=["u1", "u2"])

        payload = json.dumps(
            {
                "entries": [
                    {
                        "id": "fresh_vid",
                        "title": "Fresh",
                        "url": "https://www.youtube.com/watch?v=fresh_vid",
                    }
                ]
            }
        )

        sent_messages: list = []

        async def _send(guid, text):
            sent_messages.append((guid, text))

        with patch.object(yn, "_run_ytdlp", return_value=(0, payload, "")) as mock_ytdlp:
            asyncio.run(yn._poll_once(_send))

        # Exactly one yt-dlp invocation.
        assert mock_ytdlp.call_count == 1, "Poll must call yt-dlp once per channel, not per subscriber"
        # Both users received a notification.
        assert len(sent_messages) == 2

    def test_ytdlp_failure_does_not_crash_loop(self):
        channel_id = "UCpoll000000000000000004"
        self._seed_channel(channel_id, subscribers=["userA"])

        sent_messages: list = []

        async def _send(guid, text):
            sent_messages.append((guid, text))

        with patch.object(yn, "_run_ytdlp", return_value=(1, "", "network error")):
            # Should not raise.
            asyncio.run(yn._poll_once(_send))

        assert sent_messages == []
