# -*- coding: utf-8 -*-
"""
zip_split.py — helper for splitting many files into multiple ZIP parts.

Usage::

    from zip_split import split_zip_from_files
    parts = split_zip_from_files(file_list, out_prefix, max_bytes)
    # → [Path("prefix-part1.zip"), Path("prefix-part2.zip"), ...]
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


def split_zip_from_files(
    files: list,
    out_prefix: Path,
    max_bytes: int,
) -> list[Path]:
    """
    Package *files* into one or more ZIP archives of at most *max_bytes* each.

    Archives are written to ``out_prefix.parent`` with names
    ``<out_prefix.name>-part1.zip``, ``-part2.zip``, …

    Sizing is based on each file's **uncompressed** size, which is a safe
    over-estimate for ZIP_DEFLATED.  A single file that is larger than
    *max_bytes* will be placed alone in its own part with a warning.

    Parameters
    ----------
    files      : iterable of path-like objects (missing files are skipped)
    out_prefix : Path whose ``name`` is used as the ZIP base name
    max_bytes  : maximum uncompressed bytes per part

    Returns
    -------
    List of Path objects for the parts that were created (at least one if any
    files were present, empty list if *files* is empty / all missing).
    """
    parts: list[Path] = []
    part_idx = 1
    current_size = 0
    zf: zipfile.ZipFile | None = None
    zip_path: Path | None = None

    def _open_new_part() -> tuple[zipfile.ZipFile, Path]:
        nonlocal part_idx, current_size
        p = out_prefix.parent / "{}-part{}.zip".format(out_prefix.name, part_idx)
        part_idx += 1
        current_size = 0
        zf_new = zipfile.ZipFile(str(p), "w", compression=zipfile.ZIP_DEFLATED)
        return zf_new, p

    try:
        for fp in files:
            fp = Path(fp)
            if not fp.exists():
                log.warning("zip_split: skipping missing file %s", fp)
                continue

            fsize = fp.stat().st_size

            # Close the current part when adding this file would exceed the limit
            # (but only if the current part already has at least one file).
            if zf is not None and current_size > 0 and (current_size + fsize) > max_bytes:
                zf.close()
                zf = None

            if zf is None:
                zf, zip_path = _open_new_part()
                parts.append(zip_path)

            if fsize > max_bytes:
                log.warning(
                    "zip_split: file %s (%d bytes) exceeds max_bytes %d — "
                    "placing alone in part %d (size limit cannot be met)",
                    fp.name, fsize, max_bytes, part_idx - 1,
                )

            zf.write(str(fp), arcname=fp.name)
            current_size += fsize

        return parts
    finally:
        if zf is not None:
            zf.close()
