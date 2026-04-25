from __future__ import annotations

"""Pydantic models for the monochrome/Tidal provider.

These mirror the TypeScript interfaces defined in js/HiFi.ts of the
monochrome-music/monochrome source (js/HiFi.ts).
"""

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Artist reference (embedded inside tracks and albums)
# Source: js/HiFi.ts — TidalArtistRef interface
# ---------------------------------------------------------------------------
@dataclass
class ArtistRef:
    id: int = 0
    name: str = ""
    handle: str | None = None
    type: str = ""
    picture: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArtistRef":
        return cls(
            id=int(d.get("id", 0)),
            name=d.get("name", ""),
            handle=d.get("handle"),
            type=d.get("type", ""),
            picture=d.get("picture"),
        )


# ---------------------------------------------------------------------------
# Album reference embedded in a track
# Source: js/HiFi.ts — TidalTrackAlbumRef interface
# ---------------------------------------------------------------------------
@dataclass
class TrackAlbumRef:
    id: int = 0
    title: str = ""
    cover: str = ""
    vibrant_color: str = ""
    video_cover: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackAlbumRef":
        return cls(
            id=int(d.get("id", 0)),
            title=d.get("title", ""),
            cover=d.get("cover", ""),
            vibrant_color=d.get("vibrantColor", ""),
            video_cover=d.get("videoCover"),
        )


# ---------------------------------------------------------------------------
# MediaMetadata (quality tags)
# Source: js/HiFi.ts — TidalMediaMetadata interface
# ---------------------------------------------------------------------------
@dataclass
class MediaMetadata:
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "MediaMetadata":
        if not d:
            return cls()
        return cls(tags=d.get("tags", []))


# ---------------------------------------------------------------------------
# Track
# Source: js/HiFi.ts — TidalTrack interface
# ---------------------------------------------------------------------------
@dataclass
class Track:
    id: int = 0
    title: str = ""
    duration: int = 0
    replay_gain: float = 0.0
    peak: float = 0.0
    allow_streaming: bool = True
    stream_ready: bool = True
    pay_to_stream: bool = False
    track_number: int = 1
    volume_number: int = 1
    version: str | None = None
    popularity: int = 0
    copyright: str = ""
    url: str = ""
    isrc: str = ""
    explicit: bool = False
    audio_quality: str = ""
    audio_modes: list[str] = field(default_factory=list)
    media_metadata: MediaMetadata = field(default_factory=MediaMetadata)
    artist: ArtistRef = field(default_factory=ArtistRef)
    artists: list[ArtistRef] = field(default_factory=list)
    album: TrackAlbumRef = field(default_factory=TrackAlbumRef)
    mixes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Track":
        artist_d = d.get("artist") or {}
        artists_d = d.get("artists") or []
        if not artist_d and artists_d:
            artist_d = artists_d[0]
        album_d = d.get("album") or {}
        return cls(
            id=int(d.get("id", 0)),
            title=d.get("title", ""),
            duration=int(d.get("duration", 0)),
            replay_gain=float(d.get("replayGain", 0)),
            peak=float(d.get("peak", 0)),
            allow_streaming=bool(d.get("allowStreaming", True)),
            stream_ready=bool(d.get("streamReady", True)),
            pay_to_stream=bool(d.get("payToStream", False)),
            track_number=int(d.get("trackNumber", 1)),
            volume_number=int(d.get("volumeNumber", 1)),
            version=d.get("version"),
            popularity=int(d.get("popularity", 0)),
            copyright=d.get("copyright", ""),
            url=d.get("url", ""),
            isrc=d.get("isrc", ""),
            explicit=bool(d.get("explicit", False)),
            audio_quality=d.get("audioQuality", ""),
            audio_modes=d.get("audioModes") or [],
            media_metadata=MediaMetadata.from_dict(d.get("mediaMetadata")),
            artist=ArtistRef.from_dict(artist_d) if artist_d else ArtistRef(),
            artists=[ArtistRef.from_dict(a) for a in artists_d],
            album=TrackAlbumRef.from_dict(album_d) if album_d else TrackAlbumRef(),
            mixes=d.get("mixes") or {},
        )

    @property
    def display_title(self) -> str:
        if self.version:
            return f"{self.title} ({self.version})"
        return self.title

    @property
    def artist_names(self) -> str:
        if self.artists:
            return ", ".join(a.name for a in self.artists if a.name)
        return self.artist.name or "Unknown Artist"


# ---------------------------------------------------------------------------
# Album
# Source: js/HiFi.ts — TidalAlbum interface
# ---------------------------------------------------------------------------
@dataclass
class Album:
    id: int = 0
    title: str = ""
    duration: int = 0
    stream_ready: bool = True
    number_of_tracks: int = 0
    number_of_videos: int = 0
    number_of_volumes: int = 1
    release_date: str = ""
    copyright: str = ""
    type: str = ""
    version: str | None = None
    url: str = ""
    cover: str = ""
    vibrant_color: str = ""
    video_cover: str | None = None
    explicit: bool = False
    upc: str = ""
    popularity: int = 0
    audio_quality: str = ""
    audio_modes: list[str] = field(default_factory=list)
    media_metadata: MediaMetadata = field(default_factory=MediaMetadata)
    artist: ArtistRef | None = None
    artists: list[ArtistRef] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Album":
        artist_d = d.get("artist")
        artists_d = d.get("artists") or []
        if not artist_d and artists_d:
            artist_d = artists_d[0]
        return cls(
            id=int(d.get("id", 0)),
            title=d.get("title", ""),
            duration=int(d.get("duration", 0)),
            stream_ready=bool(d.get("streamReady", True)),
            number_of_tracks=int(d.get("numberOfTracks", 0)),
            number_of_videos=int(d.get("numberOfVideos", 0)),
            number_of_volumes=int(d.get("numberOfVolumes", 1)),
            release_date=d.get("releaseDate", ""),
            copyright=d.get("copyright", ""),
            type=d.get("type", ""),
            version=d.get("version"),
            url=d.get("url", ""),
            cover=d.get("cover", ""),
            vibrant_color=d.get("vibrantColor", ""),
            video_cover=d.get("videoCover"),
            explicit=bool(d.get("explicit", False)),
            upc=d.get("upc", ""),
            popularity=int(d.get("popularity", 0)),
            audio_quality=d.get("audioQuality", ""),
            audio_modes=d.get("audioModes") or [],
            media_metadata=MediaMetadata.from_dict(d.get("mediaMetadata")),
            artist=ArtistRef.from_dict(artist_d) if artist_d else None,
            artists=[ArtistRef.from_dict(a) for a in artists_d],
        )


# ---------------------------------------------------------------------------
# Playlist
# Source: js/HiFi.ts — TidalPlaylist (shape inferred from api.js usage)
# ---------------------------------------------------------------------------
@dataclass
class Playlist:
    uuid: str = ""
    title: str = ""
    number_of_tracks: int = 0
    number_of_videos: int = 0
    description: str = ""
    duration: int = 0
    last_updated: str = ""
    created: str = ""
    type: str = ""
    public_playlist: bool = False
    url: str = ""
    square_image: str = ""
    image: str = ""
    popularity: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Playlist":
        return cls(
            uuid=d.get("uuid", d.get("id", "")),
            title=d.get("title", d.get("name", "")),
            number_of_tracks=int(d.get("numberOfTracks", 0)),
            number_of_videos=int(d.get("numberOfVideos", 0)),
            description=d.get("description", ""),
            duration=int(d.get("duration", 0)),
            last_updated=d.get("lastUpdated", ""),
            created=d.get("created", ""),
            type=d.get("type", ""),
            public_playlist=bool(d.get("publicPlaylist", False)),
            url=d.get("url", ""),
            square_image=d.get("squareImage", ""),
            image=d.get("image", ""),
            popularity=int(d.get("popularity", 0)),
        )

    @property
    def cover_id(self) -> str:
        return self.square_image or self.image


# ---------------------------------------------------------------------------
# Artist (full profile)
# Source: js/HiFi.ts — TidalArtistProfile interface
# ---------------------------------------------------------------------------
@dataclass
class Artist:
    id: int = 0
    name: str = ""
    artist_types: list[str] = field(default_factory=list)
    url: str = ""
    picture: str | None = None
    selected_album_cover_fallback: str | None = None
    popularity: int = 0
    handle: str | None = None
    user_id: int | None = None
    spotlighted: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artist":
        return cls(
            id=int(d.get("id", 0)),
            name=d.get("name", ""),
            artist_types=d.get("artistTypes") or [],
            url=d.get("url", ""),
            picture=d.get("picture"),
            selected_album_cover_fallback=d.get("selectedAlbumCoverFallback"),
            popularity=int(d.get("popularity", 0)),
            handle=d.get("handle"),
            user_id=d.get("userId"),
            spotlighted=bool(d.get("spotlighted", False)),
        )


# ---------------------------------------------------------------------------
# SearchResult
# Source: js/api.js — search() method return value
# ---------------------------------------------------------------------------
@dataclass
class SearchResult:
    tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
    playlists: list[Playlist] = field(default_factory=list)
    total_tracks: int = 0
    total_albums: int = 0
    total_artists: int = 0
    total_playlists: int = 0


# ---------------------------------------------------------------------------
# StreamInfo
# Source: js/api.js — getStreamUrl(), normalizeTrackManifestResponse()
# ---------------------------------------------------------------------------
@dataclass
class StreamInfo:
    track_id: int = 0
    audio_quality: str = ""
    manifest_mime_type: str = ""
    manifest: str = ""               # base64-encoded manifest string
    original_track_url: str | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    replay_gain: float | None = None
    track_replay_gain: float | None = None
    track_peak_amplitude: float | None = None
    album_replay_gain: float | None = None
    album_peak_amplitude: float | None = None
    formats: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StreamInfo":
        return cls(
            track_id=int(d.get("trackId", 0)),
            audio_quality=d.get("audioQuality", ""),
            manifest_mime_type=d.get("manifestMimeType", ""),
            manifest=d.get("manifest", ""),
            original_track_url=d.get("OriginalTrackUrl") or d.get("originalTrackUrl"),
            bit_depth=d.get("bitDepth"),
            sample_rate=d.get("sampleRate"),
            replay_gain=d.get("replayGain"),
            track_replay_gain=d.get("trackReplayGain"),
            track_peak_amplitude=d.get("trackPeakAmplitude"),
            album_replay_gain=d.get("albumReplayGain"),
            album_peak_amplitude=d.get("albumPeakAmplitude"),
            formats=d.get("formats") or [],
        )
