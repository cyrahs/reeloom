from __future__ import annotations

import json

from reeloom.kernel.errors import DomainError
from reeloom.kernel.tmdb import (
    TmdbCandidateRef,
    TmdbEpisode,
    TmdbLanguage,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
    preferred_series_identity,
)
from reeloom.ports.tmdb import (
    TmdbErrorCode,
    TmdbProvider,
    TmdbProviderError,
)
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    SeriesSelected,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
)
from reeloom.runtime.state import Phase
from reeloom.runtime.tool_runtime import ToolRuntime

MAX_SEARCH_RESULTS = 10
MAX_QUERY_BYTES = 240
MAX_TMDB_ID = (1 << 31) - 1
MAX_SEASON_NUMBER = 999
_MAX_SEASON_EPISODES = 200
_MAX_EPISODE_OVERVIEW_BYTES = 400
_MAX_OBSERVATION_BYTES = 128 * 1024


def _error_observation(code: str, *, retryable: bool) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {"code": code, "retryable": retryable},
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _begin(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
) -> str | None:
    try:
        runtime.begin(call_id=call_id, tool_name=tool_name)
    except RuntimeDomainError as error:
        if error.code in {
            RuntimeErrorCode.TOOL_NOT_ALLOWED,
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
        }:
            return _error_observation(
                error.code.value,
                retryable=(
                    error.code is RuntimeErrorCode.TOOL_NOT_ALLOWED
                ),
            )
        raise
    return None


def _reject(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
    code: str,
    retryable: bool,
) -> str:
    runtime.reject(
        call_id=call_id,
        tool_name=tool_name,
        code=code,
        retryable=retryable,
    )
    return _error_observation(code, retryable=retryable)


def _provider_missing(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
) -> str:
    return _reject(
        runtime,
        call_id=call_id,
        tool_name=tool_name,
        code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
        retryable=False,
    )


def _provider_failure(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
    error: TmdbProviderError,
) -> str:
    return _reject(
        runtime,
        call_id=call_id,
        tool_name=tool_name,
        code=error.code.value,
        retryable=error.retryable,
    )


def _valid_tmdb_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_TMDB_ID


def _series_is_authorized(
    runtime: ToolRuntime,
    tmdb_id: int,
    work_type: TmdbWorkType,
) -> bool:
    reference = TmdbCandidateRef(
        work_type=work_type,
        tmdb_id=tmdb_id,
    )
    state = runtime.state
    if state.phase is Phase.IDENTIFY_SERIES:
        return reference in state.tmdb_candidates
    return (
        state.phase is Phase.MAP_EPISODES
        and state.selected_series is not None
        and state.selected_series.tmdb_id == tmdb_id
        and state.selected_work_type is work_type
    )


def _bounded_overview(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_EPISODE_OVERVIEW_BYTES:
        return value
    return encoded[:_MAX_EPISODE_OVERVIEW_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _bounded_observation(
    payload: object,
    *,
    runtime: ToolRuntime,
    call_id: str,
    tool_name: str,
) -> str:
    observation = _serialize_observation(payload)
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation


def _serialize_observation(payload: object) -> str | None:
    observation = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(observation.encode("utf-8")) > _MAX_OBSERVATION_BYTES:
        return None
    return observation


async def search_tmdb(
    runtime: ToolRuntime,
    provider: TmdbProvider | None,
    *,
    call_id: str,
    query: str,
    work_type: TmdbWorkType,
) -> str:
    tool_name = "search_tmdb"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query.encode("utf-8")) > MAX_QUERY_BYTES
        or not isinstance(work_type, TmdbWorkType)
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if work_type is not runtime.state.work_type:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.WORK_TYPE_NOT_AUTHORIZED.value,
            retryable=False,
        )
    if provider is None:
        return _provider_missing(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
        )

    try:
        candidates = await provider.search_titles(
            query=query,
            work_type=work_type,
            language=TmdbLanguage.ZH_CN,
            limit=MAX_SEARCH_RESULTS,
            include_adult=True,
        )
    except TmdbProviderError as error:
        return _provider_failure(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            error=error,
        )

    valid = (
        type(candidates) is tuple
        and len(candidates) <= MAX_SEARCH_RESULTS
        and all(
            isinstance(candidate, TmdbSearchCandidate)
            and candidate.work_type is work_type
            for candidate in candidates
        )
        and len({candidate.tmdb_id for candidate in candidates})
        == len(candidates)
    )
    if not valid:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=TmdbErrorCode.INVALID_RESPONSE.value,
            retryable=False,
        )

    payload = {
        "ok": True,
        "results": [
            {
                "tmdb_id": candidate.tmdb_id,
                "work_type": candidate.work_type.value,
                "media_type": candidate.media_type.value,
                "localized_name": candidate.localized_name,
                "original_name": candidate.original_name,
                "year": candidate.year,
                "original_language": candidate.original_language,
            }
            for candidate in candidates
        ],
    }
    observation = _serialize_observation(payload)
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    runtime.store.append(
        TmdbCandidatesObserved(
            candidates=tuple(
                candidate.reference for candidate in candidates
            )
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation


async def get_tmdb_series(
    runtime: ToolRuntime,
    provider: TmdbProvider | None,
    *,
    call_id: str,
    tmdb_id: int,
    work_type: TmdbWorkType,
    language: TmdbLanguage,
) -> str:
    tool_name = "get_tmdb_series"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    if not _valid_tmdb_id(tmdb_id) or not isinstance(
        language,
        TmdbLanguage,
    ) or not isinstance(work_type, TmdbWorkType):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if not work_type.supports_episodes:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNSUPPORTED_WORK_TYPE.value,
            retryable=False,
        )
    if not _series_is_authorized(runtime, tmdb_id, work_type):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNKNOWN_TMDB_CANDIDATE.value,
            retryable=True,
        )
    if provider is None:
        return _provider_missing(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
        )

    try:
        details = await provider.get_series(
            tmdb_id=tmdb_id,
            work_type=work_type,
            language=language,
        )
    except TmdbProviderError as error:
        return _provider_failure(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            error=error,
        )
    if (
        not isinstance(details, TmdbSeriesDetails)
        or details.tmdb_id != tmdb_id
        or details.work_type is not work_type
        or details.language is not language
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=TmdbErrorCode.INVALID_RESPONSE.value,
            retryable=False,
        )

    return _bounded_observation(
        {
            "ok": True,
            "series": {
                "tmdb_id": details.tmdb_id,
                "work_type": details.work_type.value,
                "media_type": details.media_type.value,
                "language": details.language.value,
                "localized_name": details.localized_name,
                "original_name": details.original_name,
                "first_air_year": details.first_air_year,
                "seasons": [
                    {
                        "season_number": season.season_number,
                        "episode_count": season.episode_count,
                        "name": season.name,
                    }
                    for season in details.seasons
                ],
            },
        },
        runtime=runtime,
        call_id=call_id,
        tool_name=tool_name,
    )


async def get_tmdb_season(
    runtime: ToolRuntime,
    provider: TmdbProvider | None,
    *,
    call_id: str,
    tmdb_id: int,
    work_type: TmdbWorkType,
    season_number: int,
    language: TmdbLanguage,
) -> str:
    tool_name = "get_tmdb_season"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    if (
        not _valid_tmdb_id(tmdb_id)
        or not isinstance(work_type, TmdbWorkType)
        or type(season_number) is not int
        or not 0 <= season_number <= MAX_SEASON_NUMBER
        or not isinstance(language, TmdbLanguage)
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if not work_type.supports_episodes:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNSUPPORTED_WORK_TYPE.value,
            retryable=False,
        )
    if not _series_is_authorized(runtime, tmdb_id, work_type):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNKNOWN_TMDB_CANDIDATE.value,
            retryable=True,
        )
    if provider is None:
        return _provider_missing(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
        )

    try:
        details = await provider.get_season(
            tmdb_id=tmdb_id,
            work_type=work_type,
            season_number=season_number,
            language=language,
        )
    except TmdbProviderError as error:
        return _provider_failure(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            error=error,
        )
    if not _valid_season_details(
        details,
        tmdb_id=tmdb_id,
        work_type=work_type,
        season_number=season_number,
        language=language,
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=TmdbErrorCode.INVALID_RESPONSE.value,
            retryable=False,
        )
    episode_numbers = sorted(
        episode.episode_number for episode in details.episodes
    )
    if episode_numbers != list(range(1, len(episode_numbers) + 1)):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=TmdbErrorCode.INVALID_RESPONSE.value,
            retryable=False,
        )

    payload = {
        "ok": True,
        "season": {
            "tmdb_id": details.tmdb_id,
            "work_type": details.work_type.value,
            "media_type": details.media_type.value,
            "language": details.language.value,
            "season_number": details.season_number,
            "episodes": [
                {
                    "episode_number": episode.episode_number,
                    "name": episode.name,
                    "overview": _bounded_overview(episode.overview),
                    "special_kind": episode.special_kind.value,
                }
                for episode in details.episodes
            ],
        },
    }
    observation = _serialize_observation(payload)
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    if details.episodes:
        runtime.store.append(
            TmdbSeasonCatalogObserved(
                call_id=call_id,
                tmdb_id=tmdb_id,
                work_type=work_type,
                season_number=season_number,
                episode_count=len(details.episodes),
            )
        )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation


def _valid_season_details(
    details: object,
    *,
    tmdb_id: int,
    work_type: TmdbWorkType,
    season_number: int,
    language: TmdbLanguage,
) -> bool:
    return (
        isinstance(details, TmdbSeasonDetails)
        and details.tmdb_id == tmdb_id
        and details.work_type is work_type
        and details.season_number == season_number
        and details.language is language
        and len(details.episodes) <= _MAX_SEASON_EPISODES
        and all(
            isinstance(episode, TmdbEpisode)
            for episode in details.episodes
        )
    )


async def select_series(
    runtime: ToolRuntime,
    provider: TmdbProvider | None,
    *,
    call_id: str,
    tmdb_id: int,
    work_type: TmdbWorkType,
) -> str:
    tool_name = "select_series"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    if not _valid_tmdb_id(tmdb_id) or not isinstance(
        work_type,
        TmdbWorkType,
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if not work_type.supports_episodes:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNSUPPORTED_WORK_TYPE.value,
            retryable=False,
        )
    reference = TmdbCandidateRef(
        work_type=work_type,
        tmdb_id=tmdb_id,
    )
    if reference not in runtime.state.tmdb_candidates:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNKNOWN_TMDB_CANDIDATE.value,
            retryable=True,
        )
    if provider is None:
        return _provider_missing(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
        )

    try:
        details = await provider.get_series(
            tmdb_id=tmdb_id,
            work_type=work_type,
            language=TmdbLanguage.ZH_CN,
        )
    except TmdbProviderError as error:
        return _provider_failure(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            error=error,
        )
    if (
        not isinstance(details, TmdbSeriesDetails)
        or details.tmdb_id != tmdb_id
        or details.work_type is not work_type
        or details.language is not TmdbLanguage.ZH_CN
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=TmdbErrorCode.INVALID_RESPONSE.value,
            retryable=False,
        )
    try:
        series = preferred_series_identity(details)
    except DomainError:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.SERIES_IDENTITY_UNAVAILABLE.value,
            retryable=False,
        )

    payload = {
        "ok": True,
        "selected": {
            "tmdb_id": series.tmdb_id,
            "work_type": work_type.value,
            "media_type": work_type.media_type.value,
            "title_zh_cn": series.title_zh_cn,
            "year": series.year,
        },
        "phase": Phase.MAP_EPISODES.value,
    }
    observation = _serialize_observation(payload)
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    runtime.store.append(
        SeriesSelected(series=series, work_type=work_type)
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation
