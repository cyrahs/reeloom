from __future__ import annotations

import asyncio
import json

import pytest

from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.ports.tmdb import (
    TmdbErrorCode,
    TmdbProviderError,
)
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import CandidateSnapshotCreated, RunStarted
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.state import Phase
from reeloom.runtime.store import InMemoryEventStore
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.tmdb import (
    get_tmdb_season,
    search_tmdb,
    select_series,
)


class _FakeTmdb:
    def __init__(
        self,
        *,
        search_results: tuple[TmdbSearchCandidate, ...] = (),
        series: TmdbSeriesDetails | None = None,
        season: TmdbSeasonDetails | None = None,
        error: TmdbProviderError | None = None,
    ) -> None:
        self.search_results = search_results
        self.series = series
        self.season = season
        self.error = error
        self.search_calls: list[TmdbWorkType] = []
        self.series_calls: list[int] = []

    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        del query, language
        assert include_adult is True
        self.search_calls.append(work_type)
        if self.error is not None:
            raise self.error
        return tuple(
            candidate
            for candidate in self.search_results
            if candidate.work_type is work_type
        )[:limit]

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        del language, work_type
        self.series_calls.append(tmdb_id)
        if self.error is not None:
            raise self.error
        assert self.series is not None
        return self.series

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        del tmdb_id, work_type, season_number, language
        if self.error is not None:
            raise self.error
        assert self.season is not None
        return self.season


def _runtime(
    work_type: TmdbWorkType = TmdbWorkType.ANIME,
) -> ToolRuntime:
    store = InMemoryEventStore()
    store.append(RunStarted(run_id="run-1", work_type=work_type))
    store.append(
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:test",
            candidate_count=0,
        )
    )
    return ToolRuntime(
        store=store,
        budget=RunBudget(max_tool_calls=20, max_failures=5),
        policy=PhaseToolPolicy(),
    )


def _candidate(
    tmdb_id: int,
    work_type: TmdbWorkType = TmdbWorkType.ANIME,
) -> TmdbSearchCandidate:
    return TmdbSearchCandidate(
        tmdb_id=tmdb_id,
        localized_name=f"候选 {tmdb_id}",
        original_name=f"Candidate {tmdb_id}",
        year=2020,
        original_language="ja",
        work_type=work_type,
    )


@pytest.mark.parametrize("count", (0, 1, 2))
def test_search_supports_none_single_and_ambiguous_results(
    count: int,
) -> None:
    provider = _FakeTmdb(
        search_results=tuple(
            _candidate(index) for index in range(1, count + 1)
        )
    )
    runtime = _runtime()

    result = json.loads(
        asyncio.run(
            search_tmdb(
                runtime,
                provider,
                call_id="call-search",
                query="title",
                work_type=TmdbWorkType.ANIME,
            )
        )
    )

    assert len(result["results"]) == count
    assert {
        candidate.tmdb_id
        for candidate in runtime.state.tmdb_candidates
    } == set(
        range(1, count + 1)
    )


def test_non_candidate_series_is_rejected_before_provider_call() -> None:
    provider = _FakeTmdb()
    runtime = _runtime()

    result = json.loads(
        asyncio.run(
            select_series(
                runtime,
                provider,
                call_id="call-select",
                tmdb_id=999,
                work_type=TmdbWorkType.ANIME,
            )
        )
    )

    assert result["error"]["code"] == "unknown_tmdb_candidate"
    assert provider.series_calls == []
    assert runtime.state.phase is Phase.IDENTIFY_SERIES


def test_search_filter_must_match_trusted_run_work_type() -> None:
    provider = _FakeTmdb(
        search_results=(
            _candidate(100, work_type=TmdbWorkType.MOVIE),
        )
    )
    runtime = _runtime(work_type=TmdbWorkType.ANIME)

    result = json.loads(
        asyncio.run(
            search_tmdb(
                runtime,
                provider,
                call_id="call-search",
                query="title",
                work_type=TmdbWorkType.MOVIE,
            )
        )
    )

    assert result["error"]["code"] == "work_type_not_authorized"
    assert provider.search_calls == []
    assert runtime.state.tmdb_candidates == frozenset()


def test_movie_search_returns_type_but_series_selection_is_closed() -> None:
    provider = _FakeTmdb(
        search_results=(
            _candidate(100, work_type=TmdbWorkType.MOVIE),
        )
    )
    runtime = _runtime(work_type=TmdbWorkType.MOVIE)

    search_result = json.loads(
        asyncio.run(
            search_tmdb(
                runtime,
                provider,
                call_id="call-search",
                query="movie",
                work_type=TmdbWorkType.MOVIE,
            )
        )
    )
    select_result = json.loads(
        asyncio.run(
            select_series(
                runtime,
                provider,
                call_id="call-select",
                tmdb_id=100,
                work_type=TmdbWorkType.MOVIE,
            )
        )
    )

    assert search_result["results"][0]["work_type"] == "movie"
    assert search_result["results"][0]["media_type"] == "movie"
    assert select_result["error"]["code"] == "unsupported_work_type"
    assert provider.series_calls == []
    assert runtime.state.phase is Phase.IDENTIFY_SERIES


def test_select_series_uses_zh_cn_title_and_advances_phase() -> None:
    provider = _FakeTmdb(
        search_results=(_candidate(100),),
        series=TmdbSeriesDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            localized_name="中文标题",
            original_name="Original",
            first_air_year=2020,
            seasons=(),
            work_type=TmdbWorkType.ANIME,
        ),
    )
    runtime = _runtime()
    asyncio.run(
        search_tmdb(
            runtime,
            provider,
            call_id="call-search",
            query="title",
            work_type=TmdbWorkType.ANIME,
        )
    )

    result = json.loads(
        asyncio.run(
            select_series(
                runtime,
                provider,
                call_id="call-select",
                tmdb_id=100,
                work_type=TmdbWorkType.ANIME,
            )
        )
    )

    assert result["selected"] == {
        "tmdb_id": 100,
        "work_type": "anime",
        "media_type": "tv",
        "title_zh_cn": "中文标题",
        "year": 2020,
    }
    assert runtime.state.phase is Phase.MAP_EPISODES
    assert runtime.state.selected_series is not None


def test_tmdb_network_failure_is_a_structured_observation() -> None:
    provider = _FakeTmdb(
        error=TmdbProviderError(
            TmdbErrorCode.UNAVAILABLE,
            retryable=True,
        )
    )
    runtime = _runtime()

    result = json.loads(
        asyncio.run(
            search_tmdb(
                runtime,
                provider,
                call_id="call-search",
                query="title",
                work_type=TmdbWorkType.ANIME,
            )
        )
    )

    assert result == {
        "ok": False,
        "error": {
            "code": TmdbErrorCode.UNAVAILABLE.value,
            "retryable": True,
        },
    }
    assert runtime.state.failures == 1


def test_selected_series_can_query_specials_with_hints() -> None:
    provider = _FakeTmdb(
        search_results=(_candidate(100),),
        series=TmdbSeriesDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            localized_name="中文标题",
            original_name="Original",
            first_air_year=2020,
            seasons=(),
            work_type=TmdbWorkType.ANIME,
        ),
        season=TmdbSeasonDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            season_number=0,
            episodes=(
                TmdbEpisode(
                    season_number=0,
                    episode_number=1,
                    name="OVA",
                    overview="",
                    special_kind=SpecialKind.OVA,
                ),
                TmdbEpisode(
                    season_number=0,
                    episode_number=2,
                    name="随书附赠动画",
                    overview="",
                    special_kind=SpecialKind.OAD,
                ),
            ),
            work_type=TmdbWorkType.ANIME,
        ),
    )
    runtime = _runtime()
    asyncio.run(
        search_tmdb(
            runtime,
            provider,
            call_id="call-search",
            query="title",
            work_type=TmdbWorkType.ANIME,
        )
    )
    asyncio.run(
        select_series(
            runtime,
            provider,
            call_id="call-select",
            tmdb_id=100,
            work_type=TmdbWorkType.ANIME,
        )
    )

    result = json.loads(
        asyncio.run(
            get_tmdb_season(
                runtime,
                provider,
                call_id="call-season",
                tmdb_id=100,
                work_type=TmdbWorkType.ANIME,
                season_number=0,
                language=TmdbLanguage.ZH_CN,
            )
        )
    )

    assert [
        episode["special_kind"]
        for episode in result["season"]["episodes"]
    ] == ["ova", "oad"]


def test_season_tool_rejects_sparse_episode_numbers() -> None:
    provider = _FakeTmdb(
        search_results=(_candidate(100),),
        series=TmdbSeriesDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            localized_name="中文标题",
            original_name="Original",
            first_air_year=2020,
            seasons=(),
            work_type=TmdbWorkType.ANIME,
        ),
        season=TmdbSeasonDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            season_number=1,
            episodes=(
                TmdbEpisode(
                    season_number=1,
                    episode_number=1,
                    name="E01",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
                TmdbEpisode(
                    season_number=1,
                    episode_number=3,
                    name="E03",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
            ),
            work_type=TmdbWorkType.ANIME,
        ),
    )
    runtime = _runtime()
    asyncio.run(
        search_tmdb(
            runtime,
            provider,
            call_id="call-search",
            query="title",
            work_type=TmdbWorkType.ANIME,
        )
    )
    asyncio.run(
        select_series(
            runtime,
            provider,
            call_id="call-select",
            tmdb_id=100,
            work_type=TmdbWorkType.ANIME,
        )
    )

    result = json.loads(
        asyncio.run(
            get_tmdb_season(
                runtime,
                provider,
                call_id="call-season",
                tmdb_id=100,
                work_type=TmdbWorkType.ANIME,
                season_number=1,
                language=TmdbLanguage.ZH_CN,
            )
        )
    )

    assert result["error"]["code"] == TmdbErrorCode.INVALID_RESPONSE.value
    assert runtime.state.episode_catalog_counts == ()
