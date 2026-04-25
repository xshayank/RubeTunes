from __future__ import annotations

"""Async-friendly wrapper around musicdl's MusicClient.

All blocking musicdl calls are dispatched via ``asyncio.to_thread`` so the
Rubika event loop is never blocked.  The module lazy-imports musicdl so a
missing or broken install only breaks the musicdl routes, not the whole app.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from rubetunes.providers.musicdl.config import (
    MUSICDL_DEFAULT_SOURCES,
    MUSICDL_DOWNLOAD_DIR,
    build_init_cfg,
    build_requests_overrides,
)
from rubetunes.providers.musicdl.errors import (
    MusicdlDownloadError,
    MusicdlNotInstalledError,
    MusicdlSearchError,
)
from rubetunes.providers.musicdl.models import (
    MusicdlDownloadResult,
    MusicdlSearchResult,
    MusicdlTrack,
)

__all__ = ["MusicdlClient"]

log = logging.getLogger(__name__)

AUDIO_EXTS: frozenset[str] = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"})


def _find_downloaded_file(
    dirs: list[Path],
    song_name: str,
    existing: frozenset[Path],
) -> Path | None:
    """Return the most recently modified audio file under any of *dirs*.

    Prefers files that were not present in *existing* (i.e. written during
    the download).  Falls back to a name-based match, then to the newest
    file overall.
    """
    candidates: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                candidates.append(p)
    if not candidates:
        return None

    # Prefer files that weren't there before the download
    new_candidates = [p for p in candidates if p not in existing]
    if new_candidates:
        candidates = new_candidates

    # Prefer files whose stem contains the song_name (best-effort)
    if song_name:
        name_matches = [p for p in candidates if song_name.lower() in p.stem.lower()]
        if name_matches:
            candidates = name_matches

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _import_musicdl() -> Any:
    """Lazy-import musicdl.MusicClient; raises MusicdlNotInstalledError if absent."""
    try:
        from musicdl.musicdl import MusicClient  # type: ignore[import]

        return MusicClient
    except ImportError as exc:
        raise MusicdlNotInstalledError() from exc


def _import_client_builder() -> Any:
    """Lazy-import MusicClientBuilder to access REGISTERED_MODULES."""
    try:
        from musicdl.modules import MusicClientBuilder  # type: ignore[import]

        return MusicClientBuilder
    except ImportError as exc:
        raise MusicdlNotInstalledError() from exc


class MusicdlClient:
    """Async wrapper around musicdl's ``MusicClient``.

    Usage::

        client = MusicdlClient()
        result = await client.search("Bohemian Rhapsody", limit=5)
        for track in result.tracks:
            print(track.display_title)
    """

    def __init__(
        self,
        sources: list[str] | None = None,
    ) -> None:
        self._sources: list[str] = sources or MUSICDL_DEFAULT_SOURCES or []
        self._proxy_overrides: dict = build_requests_overrides()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    def list_sources(self) -> list[str]:
        """Return all source client names currently registered in musicdl.

        This reads ``MusicClientBuilder.REGISTERED_MODULES`` at runtime so
        it always reflects the actual installed version of musicdl.
        """
        builder = _import_client_builder()
        return sorted(builder.REGISTERED_MODULES.keys())

    async def search(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ) -> MusicdlSearchResult:
        """Search for tracks across musicdl sources.

        Parameters
        ----------
        query:
            Free-text search string.
        sources:
            Override the default sources for this call only.
        limit:
            Maximum tracks to return *per source* (approximated via
            ``search_size_per_source``).
        """
        if not query:
            raise MusicdlSearchError("Search query must not be empty.")

        effective_sources = sources or self._sources

        def _blocking_search() -> dict:
            MusicClient = _import_musicdl()
            init_cfg: dict = {}
            for src in effective_sources:
                cfg = build_init_cfg(src)
                cfg["search_size_per_source"] = limit
                init_cfg[src] = cfg

            overrides: dict = {}
            if self._proxy_overrides:
                for src in effective_sources:
                    overrides[src] = self._proxy_overrides

            try:
                client = MusicClient(
                    music_sources=effective_sources,
                    init_music_clients_cfg=init_cfg,
                    requests_overrides=overrides,
                )
                return client.search(keyword=query)
            except Exception as exc:
                raise MusicdlSearchError(f"musicdl search failed: {exc}") from exc

        log.info("musicdl search | query=%r | sources=%s", query, effective_sources)
        raw_results: dict = await asyncio.to_thread(_blocking_search)

        # Normalise
        by_source: dict[str, list[MusicdlTrack]] = {}
        all_tracks: list[MusicdlTrack] = []
        for src, infos in raw_results.items():
            tracks = [MusicdlTrack.from_song_info(i) for i in (infos or [])]
            by_source[src] = tracks
            all_tracks.extend(tracks)

        return MusicdlSearchResult(
            query=query,
            tracks=all_tracks,
            by_source=by_source,
            total=len(all_tracks),
        )

    async def download(
        self,
        track: MusicdlTrack,
        dest_dir: Path | None = None,
    ) -> MusicdlDownloadResult:
        """Download a track previously returned by :meth:`search`.

        Parameters
        ----------
        track:
            A :class:`MusicdlTrack` whose ``_raw`` field holds the original
            musicdl ``SongInfo`` object.
        dest_dir:
            Override the download directory for this call.  Defaults to the
            source-specific sub-directory under ``MUSICDL_DOWNLOAD_DIR``.
        """
        if track._raw is None:
            raise MusicdlDownloadError("Cannot download a MusicdlTrack without a raw SongInfo.")

        effective_dir = dest_dir or (MUSICDL_DOWNLOAD_DIR / (track.source or "unknown"))

        def _blocking_download() -> MusicdlTrack:
            MusicClient = _import_musicdl()
            init_cfg = {track.source: build_init_cfg(track.source)}
            if dest_dir:
                init_cfg[track.source]["work_dir"] = str(effective_dir)

            overrides: dict = {}
            if self._proxy_overrides:
                overrides[track.source] = self._proxy_overrides

            try:
                client = MusicClient(
                    music_sources=[track.source],
                    init_music_clients_cfg=init_cfg,
                    requests_overrides=overrides,
                )
                downloaded = client.download(song_infos=[track._raw])
                if not downloaded:
                    raise MusicdlDownloadError(f"musicdl returned no results for track: {track.song_name!r}")
                return MusicdlTrack.from_song_info(downloaded[0])
            except MusicdlDownloadError:
                raise
            except Exception as exc:
                raise MusicdlDownloadError(f"musicdl download failed: {exc}") from exc

        log.info(
            "musicdl download | track=%r | source=%s | dest=%s",
            track.song_name,
            track.source,
            effective_dir,
        )
        effective_dir.mkdir(parents=True, exist_ok=True)

        # Snapshot audio files present BEFORE the download so we can identify
        # the file that musicdl writes (it doesn't populate file_path on SongInfo).
        if effective_dir.exists():
            existing_files: frozenset[Path] = frozenset(
                p for p in effective_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
            )
        else:
            existing_files = frozenset()

        result_track: MusicdlTrack = await asyncio.to_thread(_blocking_download)

        # If musicdl didn't populate file_path (the common case), locate the
        # newly written audio file by scanning effective_dir recursively.
        if not result_track.file_path:
            dirs_to_scan: list[Path] = [effective_dir]
            if track.source:
                source_subdir = effective_dir / track.source
                if source_subdir != effective_dir:
                    dirs_to_scan.append(source_subdir)
            resolved = _find_downloaded_file(dirs_to_scan, track.song_name, existing_files)
            if resolved:
                result_track.file_path = str(resolved)
                log.debug("musicdl: resolved file_path via disk scan → %s", resolved)

        fp = Path(result_track.file_path) if result_track.file_path else effective_dir
        return MusicdlDownloadResult(
            track=result_track,
            file_path=fp,
            success=bool(result_track.file_path),
            error="" if result_track.file_path else "No file path in result",
        )
