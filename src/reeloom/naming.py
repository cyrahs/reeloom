"""Deterministic naming.

Every destination path in the system is produced here from a TMDB identity
and an episode span. The Agent supplies neither, which is what keeps model
output out of the filesystem: it can only say *which candidate is which
episode*, never *where a file goes*.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.models import (
    EpisodeSpan,
    MediaIdentity,
    PlanError,
    SubtitleVariant,
)
from reeloom.scanner import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS

_FORBIDDEN = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_MAX_TITLE_BYTES = 160
_TMDB_TAG = re.compile(r"\{tmdb-(\d+)\}")


def sanitize_title(value: str) -> str:
    """Make a TMDB title safe as a single path component.

    TMDB text is untrusted: separators and control characters become spaces so
    a title can never introduce a directory level or escape its parent.
    """

    if not isinstance(value, str):
        raise PlanError("invalid_title", value=repr(value))

    normalized = unicodedata.normalize("NFKC", value)
    characters = [
        " " if char in _FORBIDDEN or unicodedata.category(char).startswith("C") else char
        for char in normalized
    ]
    collapsed = " ".join("".join(characters).split()).strip(" .")
    collapsed = _truncate_utf8(collapsed, _MAX_TITLE_BYTES)
    if not collapsed:
        raise PlanError("empty_title", value=value)

    stem, separator, remainder = collapsed.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        collapsed = f"{stem}_{separator}{remainder}"
    return collapsed


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def check_extension(value: str, *, subtitle: bool) -> str:
    allowed = SUBTITLE_EXTENSIONS if subtitle else VIDEO_EXTENSIONS
    canonical = value.lower()
    if canonical not in allowed:
        raise PlanError("unsupported_extension", extension=value)
    return canonical


def folder_name(identity: MediaIdentity) -> str:
    """Canonical library folder for a title, series or movie alike."""

    title = sanitize_title(identity.title)
    if not 1000 <= identity.year <= 9999:
        raise PlanError("invalid_year", year=identity.year)
    if identity.tmdb_id < 1:
        raise PlanError("invalid_tmdb_id", tmdb_id=identity.tmdb_id)
    return f"{title} ({identity.year}) {{tmdb-{identity.tmdb_id}}}"


def parse_tmdb_id(name: str) -> int | None:
    """Read the ``{tmdb-N}`` tag out of an existing library folder name."""

    match = _TMDB_TAG.search(name)
    return int(match.group(1)) if match else None


def season_name(span: EpisodeSpan) -> str:
    return f"S{span.season:02d}"


def _episode_token(span: EpisodeSpan) -> str:
    token = f"E{span.episode_start:02d}"
    if span.episode_end != span.episode_start:
        token = f"{token}-E{span.episode_end:02d}"
    return token


def episode_path(
    identity: MediaIdentity,
    span: EpisodeSpan,
    extension: str,
    *,
    variant: SubtitleVariant | None = None,
    root: str | None = None,
) -> PurePosixPath:
    """``<folder>/S01/Title S01E01[.chs].ext``.

    ``root`` overrides the folder component so episodes can join a library
    folder that already exists under a different name.
    """

    canonical = check_extension(extension, subtitle=variant is not None)
    title = sanitize_title(identity.title)
    suffix = canonical if variant is None else f".{variant.value}{canonical}"
    season = season_name(span)
    filename = f"{title} {season}{_episode_token(span)}{suffix}"
    return PurePosixPath(root or folder_name(identity), season, filename)


def movie_path(
    identity: MediaIdentity,
    extension: str,
    *,
    variant: SubtitleVariant | None = None,
    root: str | None = None,
) -> PurePosixPath:
    """``<folder>/Title (Year)[.chs].ext`` — movies have no season level."""

    canonical = check_extension(extension, subtitle=variant is not None)
    title = sanitize_title(identity.title)
    suffix = canonical if variant is None else f".{variant.value}{canonical}"
    filename = f"{title} ({identity.year}){suffix}"
    return PurePosixPath(root or folder_name(identity), filename)


@dataclass(frozen=True, slots=True)
class ExistingFolder:
    """A library folder that already holds this title."""

    name: str

    @property
    def tmdb_id(self) -> int | None:
        return parse_tmdb_id(self.name)

    @property
    def needs_tmdb_id(self) -> bool:
        return self.tmdb_id is None


def resolve_library_folder(
    identity: MediaIdentity, existing: ExistingFolder | None
) -> tuple[str, str | None]:
    """Decide the folder to write into and whether to rename an old one.

    Returns ``(target_name, rename_from)``. An existing folder that already
    carries a ``{tmdb-id}`` is left exactly as it is — adding a season must
    never reshuffle what is already on disk. An older folder without the tag
    is renamed once to pick it up.
    """

    canonical = folder_name(identity)
    if existing is None:
        return canonical, None
    if not existing.needs_tmdb_id:
        return existing.name, None
    if existing.name == canonical:
        return canonical, None
    return canonical, existing.name
