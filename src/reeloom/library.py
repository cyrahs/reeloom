"""Reading the media library.

Two questions are asked of it: does a folder for this title already exist,
and — for version replacement — what does a folder hold, episode by episode?
Both are answered deterministically; matching a folder to a title by its
``{tmdb-id}`` tag or normalized name is never a model decision.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

from reeloom.models import MediaIdentity, Root
from reeloom.naming import (
    ExistingFolder,
    folder_name,
    parse_tmdb_id,
    sanitize_title,
    span_from_name,
)
from reeloom.replace import ExistingFile
from reeloom.scanner import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, name_key

_LOGGER = logging.getLogger(__name__)

MAX_INVENTORY_FILES = 10_000
_INVENTORY_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS


def find_existing_folder(
    library_root: Path, identity: MediaIdentity
) -> ExistingFolder | None:
    """Locate the folder already holding this title, if any.

    A canonical ``{tmdb-id}`` folder always wins. When both a tagged and an
    untagged folder exist for the same title the tagged one is used and the
    old one is left alone rather than merged.
    """

    if not library_root.is_dir():
        return None

    title = sanitize_title(identity.title)
    canonical = folder_name(identity)
    tagged: str | None = None
    untagged: str | None = None

    with os.scandir(library_root) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if parse_tmdb_id(entry.name) == identity.tmdb_id:
                if tagged is None or entry.name == canonical:
                    tagged = entry.name
            elif parse_tmdb_id(entry.name) is None and _title_matches(
                entry.name, title
            ):
                untagged = entry.name

    if tagged is not None:
        if untagged is not None:
            _LOGGER.info(
                "both tagged and untagged folders exist for tmdb-%s;"
                " using %r and leaving %r alone",
                identity.tmdb_id,
                tagged,
                untagged,
            )
        return ExistingFolder(tagged)
    if untagged is not None:
        return ExistingFolder(untagged)
    return None


def _title_matches(folder: str, title: str) -> bool:
    """Match ``Title`` and ``Title (2024)`` but nothing looser."""

    key = name_key(folder)
    if key == name_key(title):
        return True
    head, separator, tail = folder.rpartition(" (")
    return bool(separator) and tail.rstrip(")").isdigit() and name_key(
        head
    ) == name_key(title)


def title_matches_folder(folder: str, identity: MediaIdentity) -> bool:
    """Deterministic name match for replacement detection in extra dirs."""

    return _title_matches(folder, sanitize_title(identity.title))


def folder_inventory(
    root: Path,
    folder: str,
    *,
    file_root: Root = Root.LIBRARY,
    extra_base: str | None = None,
) -> list[ExistingFile]:
    """Every video and subtitle inside ``<root>/<folder>``, spans parsed.

    A file whose name yields no span is still listed (with ``span=None``) —
    it can pair with a movie, but for a series it can never be touched.
    Symlinks are never followed.
    """

    base = root / folder
    if base.is_symlink() or not base.is_dir():
        return []

    results: list[ExistingFile] = []
    for current, directories, files in os.walk(base, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".")
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            if len(results) >= MAX_INVENTORY_FILES:
                return results
            path = Path(current) / name
            if path.is_symlink():
                continue
            if PurePosixPath(name).suffix.lower() not in _INVENTORY_EXTENSIONS:
                continue
            relative = PurePosixPath(folder) / path.relative_to(base).as_posix()
            results.append(
                ExistingFile(
                    root=file_root,
                    extra_base=extra_base,
                    relative_path=relative.as_posix(),
                    size_bytes=path.stat().st_size,
                    span=span_from_name(name),
                )
            )
    return results
