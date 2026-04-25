from __future__ import annotations

"""musicdl provider — custom exception types.

These translate musicdl-level failures into RubeTunes-friendly errors so callers
never need to import musicdl directly to handle errors.
"""

__all__ = [
    "MusicdlError",
    "MusicdlNotInstalledError",
    "MusicdlSearchError",
    "MusicdlDownloadError",
]


class MusicdlError(Exception):
    """Base class for all musicdl provider errors."""


class MusicdlNotInstalledError(MusicdlError):
    """Raised when the musicdl package is not installed in the current environment."""

    def __init__(self) -> None:
        super().__init__(
            "musicdl is not installed. "
            "Run: pip install musicdl==2.11.1  "
            "(and optionally install nodejs for pyexecjs-based sources)."
        )


class MusicdlSearchError(MusicdlError):
    """Raised when a musicdl search call fails."""


class MusicdlDownloadError(MusicdlError):
    """Raised when a musicdl download call fails."""
