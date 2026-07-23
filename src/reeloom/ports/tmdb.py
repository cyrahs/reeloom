from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from reeloom.kernel.tmdb import (
    TmdbLanguage,
    TmdbMovieDetails,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)


class TmdbErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "tmdb_authentication_failed"
    NOT_FOUND = "tmdb_not_found"
    RATE_LIMITED = "tmdb_rate_limited"
    UNAVAILABLE = "tmdb_unavailable"
    RESPONSE_TOO_LARGE = "tmdb_response_too_large"
    INVALID_RESPONSE = "tmdb_invalid_response"


class TmdbProviderError(RuntimeError):
    def __init__(
        self,
        code: TmdbErrorCode,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


class TmdbProvider(Protocol):
    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]: ...

    async def get_movie(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbMovieDetails: ...

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails: ...

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails: ...
