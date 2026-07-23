from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeSpan
from reeloom.kernel.schema import check_fields

_SERIES_FIELDS = frozenset({"title_zh_cn", "year", "tmdb_id"})
_FORBIDDEN_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_MAX_SAFE_TITLE_BYTES = 160
_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,8}$")
_VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mp4", ".ts", ".webm"})
_SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".sup", ".vtt"})


def _truncate_utf8(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def _sanitize_title(value: object) -> str:
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_SERIES_TITLE)

    normalized = unicodedata.normalize("NFKC", value)
    safe_characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if (
            character in _FORBIDDEN_COMPONENT_CHARACTERS
            or category.startswith("C")
        ):
            safe_characters.append(" ")
        else:
            safe_characters.append(character)

    collapsed = " ".join("".join(safe_characters).split()).strip(" .")
    collapsed = _truncate_utf8(collapsed, max_bytes=_MAX_SAFE_TITLE_BYTES)
    if not collapsed:
        raise DomainError(ErrorCode.INVALID_SERIES_TITLE)

    stem, separator, remainder = collapsed.partition(".")
    reserved_stem = stem.upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        collapsed = f"{stem}_{separator}{remainder}"
    return collapsed


def _canonical_extension(
    value: object,
    *,
    allowed: frozenset[str],
) -> str:
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_FILE_EXTENSION)

    canonical = value.lower()
    if (
        _EXTENSION_PATTERN.fullmatch(canonical) is None
        or canonical not in allowed
    ):
        raise DomainError(
            ErrorCode.INVALID_FILE_EXTENSION,
            context={"extension": value},
        )
    return canonical


@dataclass(frozen=True, slots=True)
class SeriesIdentity:
    """Canonical series identity used by the deterministic naming compiler."""

    title_zh_cn: str
    year: int
    tmdb_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "title_zh_cn", _sanitize_title(self.title_zh_cn))
        if type(self.year) is not int or not 1000 <= self.year <= 9999:
            raise DomainError(ErrorCode.INVALID_YEAR)
        if type(self.tmdb_id) is not int or self.tmdb_id < 1:
            raise DomainError(ErrorCode.INVALID_TMDB_ID)

    @classmethod
    def from_dict(cls, payload: object) -> SeriesIdentity:
        payload = check_fields(payload, _SERIES_FIELDS, field="series")
        return cls(
            title_zh_cn=payload["title_zh_cn"],  # type: ignore[arg-type]
            year=payload["year"],  # type: ignore[arg-type]
            tmdb_id=payload["tmdb_id"],  # type: ignore[arg-type]
        )


class SubtitleVariant(StrEnum):
    CHS = "chs"
    CHT = "cht"
    CHI = "chi"


def series_root_name(series: SeriesIdentity) -> str:
    if not isinstance(series, SeriesIdentity):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": "series", "expected": "SeriesIdentity"},
        )
    return f"{series.title_zh_cn} ({series.year}) {{tmdb-{series.tmdb_id}}}"


def _episode_tokens(span: EpisodeSpan) -> tuple[str, str]:
    if not isinstance(span, EpisodeSpan):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": "span", "expected": "EpisodeSpan"},
        )

    season = f"S{span.season:02d}"
    episode = f"E{span.episode_start:02d}"
    if span.episode_end != span.episode_start:
        episode = f"{episode}-E{span.episode_end:02d}"
    return season, episode


def _relative_path(
    series: SeriesIdentity,
    span: EpisodeSpan,
    *,
    suffix: str,
) -> PurePosixPath:
    root_name = series_root_name(series)
    season, episode = _episode_tokens(span)
    filename = f"{series.title_zh_cn} {season}{episode}{suffix}"
    return PurePosixPath(root_name, season, filename)


def video_relative_path(
    series: SeriesIdentity,
    span: EpisodeSpan,
    extension: object,
) -> PurePosixPath:
    """Compile a video destination; no caller-supplied path is accepted."""

    canonical_extension = _canonical_extension(
        extension,
        allowed=_VIDEO_EXTENSIONS,
    )
    return _relative_path(series, span, suffix=canonical_extension)


def subtitle_relative_path(
    series: SeriesIdentity,
    span: EpisodeSpan,
    variant: SubtitleVariant,
    extension: object,
) -> PurePosixPath:
    """Compile a subtitle destination sharing the associated video's base name."""

    if not isinstance(variant, SubtitleVariant):
        raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)
    canonical_extension = _canonical_extension(
        extension,
        allowed=_SUBTITLE_EXTENSIONS,
    )
    return _relative_path(
        series,
        span,
        suffix=f".{variant.value}{canonical_extension}",
    )
