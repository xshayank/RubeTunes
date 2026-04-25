from __future__ import annotations

"""Pydantic-style dataclass models for the musicdl provider.

These mirror the fields exposed by musicdl's ``SongInfo`` namedtuple so that
the rest of RubeTunes never has to import musicdl directly.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MusicdlTrack",
    "MusicdlSearchResult",
    "MusicdlDownloadResult",
]


@dataclass
class MusicdlTrack:
    """Represents a single track returned by a musicdl search.

    Field names follow ``SongInfo`` from musicdl's source with minor
    snake_case normalisations for consistency with the rest of RubeTunes.
    """

    # Internal musicdl reference — the raw SongInfo object.  Kept so the
    # client can pass it straight back to MusicClient.download() without
    # losing any metadata.
    _raw: Any = field(default=None, repr=False)

    song_name: str = ""
    singers: str = ""
    album: str = ""
    source: str = ""
    """The musicdl client key, e.g. 'NeteaseMusicClient'."""
    file_size: str = ""
    duration: str = ""
    song_id: str = ""
    ext: str = ""
    """File extension hint (e.g. 'flac', 'mp3')."""
    cover_url: str = ""
    lyric: str = ""
    file_path: str = ""

    @classmethod
    def from_song_info(cls, info: Any) -> MusicdlTrack:
        """Build a MusicdlTrack from a musicdl ``SongInfo`` object."""
        return cls(
            _raw=info,
            song_name=str(getattr(info, "song_name", "") or ""),
            singers=str(getattr(info, "singers", "") or ""),
            album=str(getattr(info, "album", "") or ""),
            source=str(getattr(info, "source", "") or ""),
            file_size=str(getattr(info, "file_size", "") or ""),
            duration=str(getattr(info, "duration", "") or ""),
            song_id=str(getattr(info, "song_id", "") or ""),
            ext=str(getattr(info, "ext", "") or ""),
            cover_url=str(getattr(info, "cover_url", "") or ""),
            lyric=str(getattr(info, "lyric", "") or ""),
            file_path=str(getattr(info, "file_path", "") or ""),
        )

    @property
    def display_title(self) -> str:
        if self.singers:
            return f"{self.singers} — {self.song_name}"
        return self.song_name


@dataclass
class MusicdlSearchResult:
    """A collection of tracks grouped by source returned from a search."""

    query: str = ""
    tracks: list[MusicdlTrack] = field(default_factory=list)
    """All tracks across all queried sources, flattened."""
    by_source: dict[str, list[MusicdlTrack]] = field(default_factory=dict)
    """Tracks keyed by musicdl source name."""
    total: int = 0


@dataclass
class MusicdlDownloadResult:
    """Result of a musicdl download operation."""

    track: MusicdlTrack | None = None
    """The downloaded track's metadata, or ``None`` if the download failed before a track was resolved."""
    file_path: Path = field(default_factory=Path)
    success: bool = False
    error: str = ""
