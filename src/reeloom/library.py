"""Reading the media library.

Only one question is asked of it: does a folder for this title already exist?
The answer is found deterministically — by the ``{tmdb-id}`` tag first, then
by an exact normalized title match — so joining a season to an existing folder
is never a model decision.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from reeloom.models import MediaIdentity
from reeloom.naming import ExistingFolder, folder_name, parse_tmdb_id, sanitize_title
from reeloom.scanner import name_key

_LOGGER = logging.getLogger(__name__)


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
