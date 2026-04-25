"""Dataclass models for Spotify metadata responses.

These are lightweight dataclasses (no external dependencies) that match the
structures returned by the pathfinder GraphQL operations used in this port.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SpotifyArtistRef:
    """A minimal artist reference (id + name)."""
    id:   str = ""
    name: str = ""


@dataclass
class SpotifyCover:
    """Cover art URLs at three resolutions."""
    small:  str = ""
    medium: str = ""
    large:  str = ""


@dataclass
class SpotifyAlbumRef:
    """Album reference embedded in a track."""
    id:           str = ""
    name:         str = ""
    released:     str = ""
    year:         int | None = None
    tracks:       int = 0
    artists:      str = ""
    label:        str = ""


@dataclass
class SpotifyTrack:
    """Full track metadata returned by ``getTrack`` / ``filter_track``."""
    id:          str              = ""
    name:        str              = ""
    artists:     str              = ""
    album:       SpotifyAlbumRef | None = None
    duration:    str              = ""
    track:       int              = 1
    disc:        int              = 1
    discs:       int              = 1
    copyright:   str              = ""
    plays:       str              = ""
    cover:       SpotifyCover | None = None
    is_explicit: bool             = False

    @classmethod
    def from_dict(cls, d: dict) -> "SpotifyTrack":
        album_d = d.get("album") or {}
        cover_d = d.get("cover") or {}
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            artists=d.get("artists", ""),
            album=SpotifyAlbumRef(
                id=album_d.get("id", ""),
                name=album_d.get("name", ""),
                released=album_d.get("released", ""),
                year=album_d.get("year"),
                tracks=album_d.get("tracks", 0),
                artists=album_d.get("artists", ""),
                label=album_d.get("label", ""),
            ) if album_d else None,
            duration=d.get("duration", ""),
            track=d.get("track", 1),
            disc=d.get("disc", 1),
            discs=d.get("discs", 1),
            copyright=d.get("copyright", ""),
            plays=d.get("plays", ""),
            cover=SpotifyCover(
                small=cover_d.get("small", ""),
                medium=cover_d.get("medium", ""),
                large=cover_d.get("large", ""),
            ) if cover_d else None,
            is_explicit=d.get("is_explicit", False),
        )


@dataclass
class SpotifyAlbumTrack:
    """A track entry inside an album."""
    id:          str       = ""
    name:        str       = ""
    artists:     str       = ""
    artist_ids:  list[str] = field(default_factory=list)
    duration:    str       = ""
    plays:       str       = ""
    is_explicit: bool      = False
    disc_number: int       = 1


@dataclass
class SpotifyAlbum:
    """Full album metadata returned by ``getAlbum`` / ``filter_album``."""
    id:           str                  = ""
    name:         str                  = ""
    artists:      str                  = ""
    cover:        str                  = ""
    release_date: str                  = ""
    count:        int                  = 0
    label:        str                  = ""
    discs:        int                  = 1
    tracks:       list[SpotifyAlbumTrack] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SpotifyAlbum":
        tracks = [
            SpotifyAlbumTrack(
                id=t.get("id", ""),
                name=t.get("name", ""),
                artists=t.get("artists", ""),
                artist_ids=t.get("artistIds", []),
                duration=t.get("duration", ""),
                plays=t.get("plays", ""),
                is_explicit=t.get("is_explicit", False),
                disc_number=t.get("disc_number", 1),
            )
            for t in (d.get("tracks") or [])
        ]
        discs_d = d.get("discs") or {}
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            artists=d.get("artists", ""),
            cover=d.get("cover") or "",
            release_date=d.get("releaseDate", ""),
            count=d.get("count", 0),
            label=d.get("label", ""),
            discs=discs_d.get("totalCount", 1) if isinstance(discs_d, dict) else 1,
            tracks=tracks,
        )


@dataclass
class SpotifyPlaylistTrack:
    """A track entry inside a playlist."""
    id:           str       = ""
    cover:        str       = ""
    title:        str       = ""
    artist:       str       = ""
    artist_ids:   list[str] = field(default_factory=list)
    plays:        str | None = None
    status:       str | None = None
    album:        str        = ""
    album_artist: str        = ""
    album_id:     str        = ""
    duration:     str        = ""
    is_explicit:  bool       = False
    disc_number:  int        = 0


@dataclass
class SpotifyPlaylistOwner:
    name:   str        = ""
    avatar: str | None = None


@dataclass
class SpotifyPlaylist:
    """Full playlist metadata returned by ``fetchPlaylist`` / ``filter_playlist``."""
    id:          str                      = ""
    name:        str                      = ""
    description: str                      = ""
    owner:       SpotifyPlaylistOwner | None = None
    cover:       str | None               = None
    followers:   float | None             = None
    count:       int                      = 0
    tracks:      list[SpotifyPlaylistTrack] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SpotifyPlaylist":
        owner_d = d.get("owner") or {}
        tracks = [
            SpotifyPlaylistTrack(
                id=t.get("id", ""),
                cover=t.get("cover") or "",
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                artist_ids=t.get("artistIds", []),
                plays=t.get("plays"),
                status=t.get("status"),
                album=t.get("album", ""),
                album_artist=t.get("albumArtist", ""),
                album_id=t.get("albumId", ""),
                duration=t.get("duration", ""),
                is_explicit=t.get("is_explicit", False),
                disc_number=t.get("disc_number", 0),
            )
            for t in (d.get("tracks") or [])
        ]
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            owner=SpotifyPlaylistOwner(
                name=owner_d.get("name", ""),
                avatar=owner_d.get("avatar"),
            ) if owner_d else None,
            cover=d.get("cover"),
            followers=d.get("followers"),
            count=d.get("count", 0),
            tracks=tracks,
        )


@dataclass
class SpotifyArtist:
    """Artist metadata returned by ``queryArtistOverview``."""
    id:        str       = ""
    name:      str       = ""
    image_url: str       = ""
    biography: str       = ""
    followers: int       = 0
    listeners: int       = 0
    verified:  bool      = False
    top_tracks: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SpotifyArtist":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            image_url=d.get("image_url", ""),
            biography=d.get("biography", ""),
            followers=d.get("followers", 0),
            listeners=d.get("listeners", 0),
            verified=d.get("verified", False),
            top_tracks=d.get("top_tracks", []),
        )


@dataclass
class SpotifySearchTrack:
    """A track result from ``searchDesktop``."""
    track_id: str       = ""
    title:    str       = ""
    artists:  list[str] = field(default_factory=list)
    album:    str       = ""
    duration: str       = ""
    url:      str       = ""
