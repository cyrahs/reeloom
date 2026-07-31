from __future__ import annotations

import json
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

import httpx

from reeloom.kernel.errors import DomainError
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbMovieDetails,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeasonSummary,
    TmdbSeriesDetails,
    TmdbWorkType,
    classify_special_kind,
)
from reeloom.ports.tmdb import TmdbErrorCode, TmdbProviderError

_BASE_URL = "https://api.themoviedb.org/3"
_MAX_SEARCH_RESULTS = 20
_MAX_SEASONS = 100
_MAX_EPISODES = 500
_MAX_GENRES = 32
_ANIMATION_GENRE_ID = 16


def _title_key(value: str) -> str:
    return "".join(
        unicodedata.normalize("NFKC", value).casefold().split()
    )


def _is_exact_title_match(
    query: str,
    candidate: TmdbSearchCandidate,
) -> bool:
    query_key = _title_key(query)
    return bool(query_key) and query_key in {
        _title_key(candidate.localized_name),
        _title_key(candidate.original_name),
    }


@dataclass(frozen=True, slots=True)
class TmdbHttpLimits:
    timeout_seconds: float = 5.0
    max_response_bytes: int = 1_000_000
    cache_ttl_seconds: float = 600.0
    max_cache_entries: int = 128

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 30
            or type(self.max_response_bytes) is not int
            or not 1_024 <= self.max_response_bytes <= 2_000_000
            or not isinstance(self.cache_ttl_seconds, (int, float))
            or isinstance(self.cache_ttl_seconds, bool)
            or not 0 <= self.cache_ttl_seconds <= 3_600
            or type(self.max_cache_entries) is not int
            or not 1 <= self.max_cache_entries <= 1_024
        ):
            raise ValueError("invalid TMDB HTTP limits")


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    body: bytes


class TmdbHttpAdapter:
    """The only business-network adapter; all requests target TMDB API v3."""

    def __init__(
        self,
        *,
        api_key: str,
        limits: TmdbHttpLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 512
        ):
            raise ValueError("api_key must be a non-empty bounded string")
        self.__api_key = api_key
        self._limits = limits or TmdbHttpLimits()
        self._clock = clock
        self._cache: OrderedDict[
            tuple[str, tuple[tuple[str, str], ...]],
            _CacheEntry,
        ] = OrderedDict()
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "reeloom/0.1",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            "TmdbHttpAdapter("
            f"timeout_seconds={self._limits.timeout_seconds}, "
            f"max_response_bytes={self._limits.max_response_bytes})"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str],
    ) -> Mapping[str, object]:
        cache_key = (path, tuple(sorted(params.items())))
        now = self._clock()
        cached = self._cache.get(cache_key)
        cache_miss = cached is None or cached.expires_at <= now
        if cached is not None and cached.expires_at > now:
            self._cache.move_to_end(cache_key)
            body = cached.body
        else:
            if cached is not None:
                del self._cache[cache_key]
            body = await self._request(path, params=params)

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
        if cache_miss and self._limits.cache_ttl_seconds > 0:
            self._cache[cache_key] = _CacheEntry(
                expires_at=now + self._limits.cache_ttl_seconds,
                body=body,
            )
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._limits.max_cache_entries:
                self._cache.popitem(last=False)
        return payload

    async def _request(
        self,
        path: str,
        *,
        params: Mapping[str, str],
    ) -> bytes:
        request_params = {**params, "api_key": self.__api_key}
        try:
            async with self._client.stream(
                "GET",
                path,
                params=request_params,
                timeout=self._limits.timeout_seconds,
            ) as response:
                self._raise_for_status(response.status_code)
                content_length = response.headers.get("content-length")
                if (
                    content_length is not None
                    and content_length.isdigit()
                    and int(content_length) > self._limits.max_response_bytes
                ):
                    raise TmdbProviderError(
                        TmdbErrorCode.RESPONSE_TOO_LARGE,
                        retryable=False,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._limits.max_response_bytes:
                        raise TmdbProviderError(
                            TmdbErrorCode.RESPONSE_TOO_LARGE,
                            retryable=False,
                        )
                return bytes(body)
        except TmdbProviderError:
            raise
        except httpx.DecodingError:
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None
        except httpx.TimeoutException:
            raise TmdbProviderError(
                TmdbErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        except httpx.TransportError:
            raise TmdbProviderError(
                TmdbErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            code = TmdbErrorCode.AUTHENTICATION_FAILED
            retryable = False
        elif status_code == 404:
            code = TmdbErrorCode.NOT_FOUND
            retryable = False
        elif status_code == 429:
            code = TmdbErrorCode.RATE_LIMITED
            retryable = True
        else:
            code = TmdbErrorCode.UNAVAILABLE
            retryable = status_code >= 500
        raise TmdbProviderError(code, retryable=retryable)

    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > 240
            or not isinstance(work_type, TmdbWorkType)
            or not isinstance(language, TmdbLanguage)
            or type(limit) is not int
            or not 1 <= limit <= _MAX_SEARCH_RESULTS
            or type(include_adult) is not bool
        ):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
        payload = await self._get_json(
            f"/search/{work_type.media_type.value}",
            params={
                "query": query,
                "language": language.value,
                "include_adult": str(include_adult).lower(),
                "page": "1",
            },
        )
        raw_results = payload.get("results")
        if (
            not isinstance(raw_results, list)
            or len(raw_results) > _MAX_SEARCH_RESULTS
        ):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
        try:
            candidates = tuple(
                self._parse_search_candidate(
                    item,
                    work_type=work_type,
                )
                for item in raw_results
            )
            if work_type is TmdbWorkType.ANIME:
                candidates = tuple(
                    candidate
                    for candidate, genre_ids in candidates
                    if _ANIMATION_GENRE_ID in genre_ids
                    or (
                        not genre_ids
                        and _is_exact_title_match(query, candidate)
                    )
                )
            else:
                candidates = tuple(
                    candidate for candidate, _ in candidates
                )
            return candidates[:limit]
        except (DomainError, KeyError, TypeError, ValueError):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None

    async def get_movie(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbMovieDetails:
        self._validate_id_and_language(tmdb_id, language)
        if work_type is not TmdbWorkType.MOVIE:
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
        payload = await self._get_json(
            f"/movie/{tmdb_id}",
            params={"language": language.value},
        )
        try:
            response_id = self._required_int(payload, "id", minimum=1)
            if response_id != tmdb_id:
                raise ValueError("mismatched movie id")
            return TmdbMovieDetails(
                tmdb_id=response_id,
                language=language,
                localized_title=self._optional_string(payload, "title"),
                original_title=self._optional_string(
                    payload,
                    "original_title",
                ),
                release_year=self._year(
                    self._optional_string(payload, "release_date")
                ),
                original_language=self._optional_string(
                    payload,
                    "original_language",
                ),
                adult=self._required_bool(payload, "adult"),
                genre_ids=self._parse_detail_genre_ids(payload),
                work_type=work_type,
            )
        except (DomainError, KeyError, TypeError, ValueError):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        self._validate_id_and_language(tmdb_id, language)
        self._validate_series_work_type(work_type)
        payload = await self._get_json(
            f"/tv/{tmdb_id}",
            params={"language": language.value},
        )
        try:
            response_id = self._required_int(payload, "id", minimum=1)
            if response_id != tmdb_id:
                raise ValueError("mismatched series id")
            genre_ids = self._parse_detail_genre_ids(payload)
            if (
                work_type is TmdbWorkType.ANIME
                and genre_ids
                and _ANIMATION_GENRE_ID not in genre_ids
            ):
                raise ValueError("series is not animation")
            raw_seasons = payload.get("seasons", [])
            if (
                not isinstance(raw_seasons, list)
                or len(raw_seasons) > _MAX_SEASONS
            ):
                raise ValueError("invalid seasons")
            seasons = tuple(
                TmdbSeasonSummary(
                    season_number=self._required_int(
                        item,
                        "season_number",
                        minimum=0,
                    ),
                    episode_count=self._required_int(
                        item,
                        "episode_count",
                        minimum=0,
                    ),
                    name=self._optional_string(item, "name"),
                )
                for item in raw_seasons
            )
            return TmdbSeriesDetails(
                tmdb_id=response_id,
                language=language,
                localized_name=self._optional_string(payload, "name"),
                original_name=self._optional_string(
                    payload,
                    "original_name",
                ),
                first_air_year=self._year(
                    self._optional_string(payload, "first_air_date")
                ),
                seasons=tuple(
                    sorted(
                        seasons,
                        key=lambda season: season.season_number,
                    )
                ),
                work_type=work_type,
            )
        except (DomainError, KeyError, TypeError, ValueError):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        self._validate_id_and_language(tmdb_id, language)
        self._validate_series_work_type(work_type)
        if type(season_number) is not int or not 0 <= season_number <= 999:
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
        payload = await self._get_json(
            f"/tv/{tmdb_id}/season/{season_number}",
            params={"language": language.value},
        )
        try:
            response_season = self._required_int(
                payload,
                "season_number",
                minimum=0,
            )
            if response_season != season_number:
                raise ValueError("mismatched season")
            raw_episodes = payload.get("episodes")
            if (
                not isinstance(raw_episodes, list)
                or len(raw_episodes) > _MAX_EPISODES
            ):
                raise TypeError("invalid episodes")
            episodes = tuple(
                self._parse_episode(
                    item,
                    expected_tmdb_id=tmdb_id,
                    expected_season=season_number,
                )
                for item in raw_episodes
            )
            return TmdbSeasonDetails(
                tmdb_id=tmdb_id,
                language=language,
                season_number=season_number,
                episodes=tuple(
                    sorted(
                        episodes,
                        key=lambda episode: episode.episode_number,
                    )
                ),
                work_type=work_type,
            )
        except (DomainError, KeyError, TypeError, ValueError):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            ) from None

    @classmethod
    def _parse_search_candidate(
        cls,
        value: object,
        *,
        work_type: TmdbWorkType,
    ) -> tuple[TmdbSearchCandidate, tuple[int, ...]]:
        item = cls._object(value)
        if work_type is TmdbWorkType.MOVIE:
            localized_key = "title"
            original_key = "original_title"
            date_key = "release_date"
        else:
            localized_key = "name"
            original_key = "original_name"
            date_key = "first_air_date"
        genre_ids = cls._parse_search_genre_ids(item)
        return (
            TmdbSearchCandidate(
                tmdb_id=cls._required_int(item, "id", minimum=1),
                localized_name=cls._optional_string(
                    item,
                    localized_key,
                ),
                original_name=cls._optional_string(
                    item,
                    original_key,
                ),
                year=cls._year(
                    cls._optional_string(item, date_key)
                ),
                original_language=cls._optional_string(
                    item,
                    "original_language",
                ),
                work_type=work_type,
            ),
            genre_ids,
        )

    @classmethod
    def _parse_search_genre_ids(
        cls,
        value: object,
    ) -> tuple[int, ...]:
        item = cls._object(value)
        raw_genres = item.get("genre_ids")
        if (
            not isinstance(raw_genres, list)
            or len(raw_genres) > _MAX_GENRES
            or any(
                type(genre_id) is not int or genre_id < 1
                for genre_id in raw_genres
            )
            or len(set(raw_genres)) != len(raw_genres)
        ):
            raise TypeError("invalid genre ids")
        return tuple(raw_genres)

    @classmethod
    def _parse_detail_genre_ids(
        cls,
        value: object,
    ) -> tuple[int, ...]:
        item = cls._object(value)
        raw_genres = item.get("genres")
        if not isinstance(raw_genres, list) or len(raw_genres) > _MAX_GENRES:
            raise TypeError("invalid genres")
        genre_ids = tuple(
            cls._required_int(genre, "id", minimum=1)
            for genre in raw_genres
        )
        if len(set(genre_ids)) != len(genre_ids):
            raise TypeError("duplicate genres")
        return genre_ids

    @classmethod
    def _parse_episode(
        cls,
        value: object,
        *,
        expected_tmdb_id: int,
        expected_season: int,
    ) -> TmdbEpisode:
        item = cls._object(value)
        if (
            cls._required_int(item, "show_id", minimum=1)
            != expected_tmdb_id
        ):
            raise ValueError("mismatched series")
        season_number = cls._required_int(
            item,
            "season_number",
            minimum=0,
        )
        if season_number != expected_season:
            raise ValueError("mismatched season")
        name = cls._optional_string(item, "name")
        overview = cls._optional_string(item, "overview")
        return TmdbEpisode(
            season_number=season_number,
            episode_number=cls._required_int(
                item,
                "episode_number",
                minimum=1,
            ),
            name=name,
            overview=overview,
            special_kind=classify_special_kind(name, overview),
        )

    @staticmethod
    def _object(value: object) -> Mapping[str, object]:
        if not isinstance(value, dict) or not all(
            isinstance(key, str) for key in value
        ):
            raise TypeError("expected object")
        return value

    @classmethod
    def _required_int(
        cls,
        value: object,
        key: str,
        *,
        minimum: int,
    ) -> int:
        item = cls._object(value)
        result = item[key]
        if type(result) is not int or result < minimum:
            raise TypeError("expected bounded int")
        return result

    @classmethod
    def _required_bool(
        cls,
        value: object,
        key: str,
    ) -> bool:
        item = cls._object(value)
        result = item[key]
        if type(result) is not bool:
            raise TypeError("expected bool")
        return result

    @classmethod
    def _optional_string(
        cls,
        value: object,
        key: str,
    ) -> str:
        item = cls._object(value)
        result = item.get(key, "")
        if result is None:
            return ""
        if not isinstance(result, str):
            raise TypeError("expected string")
        return result

    @staticmethod
    def _year(value: str) -> int | None:
        if not value:
            return None
        year = date.fromisoformat(value).year
        if not 1000 <= year <= 9999:
            raise ValueError("invalid year")
        return year

    @staticmethod
    def _validate_id_and_language(
        tmdb_id: int,
        language: TmdbLanguage,
    ) -> None:
        if (
            type(tmdb_id) is not int
            or tmdb_id < 1
            or not isinstance(language, TmdbLanguage)
        ):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )

    @staticmethod
    def _validate_series_work_type(work_type: object) -> None:
        if (
            not isinstance(work_type, TmdbWorkType)
            or not work_type.supports_episodes
        ):
            raise TmdbProviderError(
                TmdbErrorCode.INVALID_RESPONSE,
                retryable=False,
            )
