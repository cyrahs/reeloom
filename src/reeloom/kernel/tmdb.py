from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import MovieIdentity, SeriesIdentity
from reeloom.kernel.specials import SpecialKind

_MAX_TITLE_BYTES = 240
_MAX_OVERVIEW_BYTES = 1_000
_SPECIAL_TOKEN_PATTERN = re.compile(r"(?<![a-z0-9])(ova|oad)(?![a-z0-9])")
_LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}$")
_POSTER_PATH_PATTERN = re.compile(
    r"^/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg)$", re.IGNORECASE
)


class TmdbLanguage(StrEnum):
    ZH_CN = "zh-CN"
    EN_US = "en-US"


class TmdbMediaType(StrEnum):
    TV = "tv"
    MOVIE = "movie"


class TmdbWorkType(StrEnum):
    """Trusted archive category mapped onto a TMDB media namespace."""

    ANIME = "anime"
    TV_SERIES = "tv_series"
    MOVIE = "movie"

    @property
    def media_type(self) -> TmdbMediaType:
        if self is TmdbWorkType.MOVIE:
            return TmdbMediaType.MOVIE
        return TmdbMediaType.TV

    @property
    def supports_episodes(self) -> bool:
        return self is not TmdbWorkType.MOVIE


def _bounded_text(
    value: object,
    *,
    max_bytes: int,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_TMDB_DATA)
    normalized = unicodedata.normalize("NFKC", value)
    visible = "".join(
        character
        if not unicodedata.category(character).startswith("C")
        else "\N{REPLACEMENT CHARACTER}"
        for character in normalized
    ).strip()
    if not visible and not allow_empty:
        raise DomainError(ErrorCode.INVALID_TMDB_DATA)
    encoded = visible.encode("utf-8")
    if len(encoded) > max_bytes:
        visible = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return visible


def _validate_tmdb_id(value: object) -> int:
    if type(value) is not int or value < 1:
        raise DomainError(ErrorCode.INVALID_TMDB_ID)
    return value


def _validate_year(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1000 <= value <= 9999:
        raise DomainError(ErrorCode.INVALID_TMDB_DATA)
    return value


def validate_tmdb_poster_path(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or _POSTER_PATH_PATTERN.fullmatch(value) is None
    ):
        raise DomainError(ErrorCode.INVALID_TMDB_DATA)
    return value


@dataclass(frozen=True, slots=True)
class TmdbCandidateRef:
    work_type: TmdbWorkType
    tmdb_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.work_type, TmdbWorkType):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(self, "tmdb_id", _validate_tmdb_id(self.tmdb_id))


@dataclass(frozen=True, slots=True)
class TmdbSearchCandidate:
    tmdb_id: int
    localized_name: str
    original_name: str
    year: int | None
    original_language: str
    work_type: TmdbWorkType

    def __post_init__(self) -> None:
        object.__setattr__(self, "tmdb_id", _validate_tmdb_id(self.tmdb_id))
        object.__setattr__(
            self,
            "localized_name",
            _bounded_text(
                self.localized_name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "original_name",
            _bounded_text(
                self.original_name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        if not self.localized_name and not self.original_name:
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "year",
            _validate_year(self.year),
        )
        if (
            not isinstance(self.original_language, str)
            or _LANGUAGE_CODE_PATTERN.fullmatch(self.original_language)
            is None
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        if not isinstance(self.work_type, TmdbWorkType):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)

    @property
    def media_type(self) -> TmdbMediaType:
        return self.work_type.media_type

    @property
    def reference(self) -> TmdbCandidateRef:
        return TmdbCandidateRef(
            work_type=self.work_type,
            tmdb_id=self.tmdb_id,
        )


@dataclass(frozen=True, slots=True)
class TmdbMovieDetails:
    tmdb_id: int
    language: TmdbLanguage
    localized_title: str
    original_title: str
    release_year: int | None
    original_language: str
    adult: bool
    genre_ids: tuple[int, ...]
    work_type: TmdbWorkType
    poster_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tmdb_id", _validate_tmdb_id(self.tmdb_id))
        if not isinstance(self.language, TmdbLanguage):
            raise DomainError(ErrorCode.INVALID_TMDB_LANGUAGE)
        if self.work_type is not TmdbWorkType.MOVIE:
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "localized_title",
            _bounded_text(
                self.localized_title,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "original_title",
            _bounded_text(
                self.original_title,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        if not self.localized_title and not self.original_title:
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "release_year",
            _validate_year(self.release_year),
        )
        if (
            not isinstance(self.original_language, str)
            or _LANGUAGE_CODE_PATTERN.fullmatch(self.original_language)
            is None
            or type(self.adult) is not bool
            or not isinstance(self.genre_ids, tuple)
            or len(self.genre_ids) > 32
            or any(
                type(genre_id) is not int or genre_id < 1
                for genre_id in self.genre_ids
            )
            or len(set(self.genre_ids)) != len(self.genre_ids)
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "poster_path",
            validate_tmdb_poster_path(self.poster_path),
        )

    @property
    def media_type(self) -> TmdbMediaType:
        return self.work_type.media_type


@dataclass(frozen=True, slots=True)
class TmdbSeasonSummary:
    season_number: int
    episode_count: int
    name: str

    def __post_init__(self) -> None:
        if (
            type(self.season_number) is not int
            or self.season_number < 0
            or type(self.episode_count) is not int
            or self.episode_count < 0
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "name",
            _bounded_text(
                self.name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class TmdbSeriesDetails:
    tmdb_id: int
    language: TmdbLanguage
    localized_name: str
    original_name: str
    first_air_year: int | None
    seasons: tuple[TmdbSeasonSummary, ...]
    work_type: TmdbWorkType
    poster_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tmdb_id", _validate_tmdb_id(self.tmdb_id))
        if not isinstance(self.language, TmdbLanguage):
            raise DomainError(ErrorCode.INVALID_TMDB_LANGUAGE)
        if (
            not isinstance(self.work_type, TmdbWorkType)
            or not self.work_type.supports_episodes
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "poster_path",
            validate_tmdb_poster_path(self.poster_path),
        )
        object.__setattr__(
            self,
            "localized_name",
            _bounded_text(
                self.localized_name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "original_name",
            _bounded_text(
                self.original_name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        if not self.localized_name and not self.original_name:
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "first_air_year",
            _validate_year(self.first_air_year),
        )
        if (
            not isinstance(self.seasons, tuple)
            or len(self.seasons) > 100
            or any(
                not isinstance(season, TmdbSeasonSummary)
                for season in self.seasons
            )
            or len(
                {season.season_number for season in self.seasons}
            )
            != len(self.seasons)
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)

    @property
    def media_type(self) -> TmdbMediaType:
        return self.work_type.media_type


@dataclass(frozen=True, slots=True)
class TmdbEpisode:
    season_number: int
    episode_number: int
    name: str
    overview: str
    special_kind: SpecialKind

    def __post_init__(self) -> None:
        if (
            type(self.season_number) is not int
            or self.season_number < 0
            or type(self.episode_number) is not int
            or self.episode_number < 1
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        object.__setattr__(
            self,
            "name",
            _bounded_text(
                self.name,
                max_bytes=_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "overview",
            _bounded_text(
                self.overview,
                max_bytes=_MAX_OVERVIEW_BYTES,
                allow_empty=True,
            ),
        )
        if not isinstance(self.special_kind, SpecialKind):
            raise DomainError(ErrorCode.INVALID_SPECIAL_KIND)


@dataclass(frozen=True, slots=True)
class TmdbSeasonDetails:
    tmdb_id: int
    language: TmdbLanguage
    season_number: int
    episodes: tuple[TmdbEpisode, ...]
    work_type: TmdbWorkType

    def __post_init__(self) -> None:
        object.__setattr__(self, "tmdb_id", _validate_tmdb_id(self.tmdb_id))
        if not isinstance(self.language, TmdbLanguage):
            raise DomainError(ErrorCode.INVALID_TMDB_LANGUAGE)
        if (
            not isinstance(self.work_type, TmdbWorkType)
            or not self.work_type.supports_episodes
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        if type(self.season_number) is not int or self.season_number < 0:
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)
        if (
            not isinstance(self.episodes, tuple)
            or len(self.episodes) > 500
            or any(
                not isinstance(episode, TmdbEpisode)
                or episode.season_number != self.season_number
                for episode in self.episodes
            )
            or len(
                {episode.episode_number for episode in self.episodes}
            )
            != len(self.episodes)
        ):
            raise DomainError(ErrorCode.INVALID_TMDB_DATA)

    @property
    def media_type(self) -> TmdbMediaType:
        return self.work_type.media_type


def classify_special_kind(name: str, overview: str) -> SpecialKind:
    """Extract only explicit OVA/OAD evidence from bounded TMDB text."""

    bounded_name = _bounded_text(
        name,
        max_bytes=_MAX_TITLE_BYTES,
        allow_empty=True,
    )
    bounded_overview = _bounded_text(
        overview,
        max_bytes=_MAX_OVERVIEW_BYTES,
        allow_empty=True,
    )
    combined = f"{bounded_name} {bounded_overview}".casefold()
    token_match = _SPECIAL_TOKEN_PATTERN.search(combined)
    if token_match is not None:
        return (
            SpecialKind.OVA
            if token_match.group(1) == "ova"
            else SpecialKind.OAD
        )

    if any(
        marker in combined
        for marker in (
            "original animation dvd",
            "原创动画光盘",
            "随书附赠动画",
            "随书附送动画",
        )
    ):
        return SpecialKind.OAD
    if any(
        marker in combined
        for marker in (
            "original video animation",
            "原创视频动画",
            "原创动画录像",
        )
    ):
        return SpecialKind.OVA
    return SpecialKind.UNKNOWN


def preferred_series_identity(details: TmdbSeriesDetails) -> SeriesIdentity:
    """Prefer the zh-CN localized name, then fall back to the original name."""

    if details.language is not TmdbLanguage.ZH_CN:
        raise DomainError(ErrorCode.INVALID_TMDB_LANGUAGE)
    if details.first_air_year is None:
        raise DomainError(ErrorCode.INVALID_YEAR)
    title = details.localized_name or details.original_name
    return SeriesIdentity(
        title_zh_cn=title,
        year=details.first_air_year,
        tmdb_id=details.tmdb_id,
    )


def preferred_movie_identity(details: TmdbMovieDetails) -> MovieIdentity:
    """Derive the only path-authoritative Movie identity from details."""

    if (
        not isinstance(details, TmdbMovieDetails)
        or details.language is not TmdbLanguage.ZH_CN
        or details.release_year is None
    ):
        raise DomainError(ErrorCode.INVALID_TMDB_DATA)
    return MovieIdentity(
        title_zh_cn=details.localized_title or details.original_title,
        release_year=details.release_year,
        tmdb_id=details.tmdb_id,
    )
