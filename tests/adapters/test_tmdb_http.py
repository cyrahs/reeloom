from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from reeloom.adapters.tmdb import TmdbHttpAdapter, TmdbHttpLimits
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import TmdbLanguage, TmdbWorkType
from reeloom.ports.tmdb import TmdbErrorCode, TmdbProviderError

_CONTRACT_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "tmdb_api_v3_contract.json"
)


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def test_search_uses_fixed_tmdb_endpoint_and_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(
            {
                "results": [
                    {
                        "id": 100,
                        "name": "葬送的芙莉莲",
                        "original_name": "葬送のフリーレン",
                        "first_air_date": "2023-09-29",
                        "original_language": "ja",
                        "genre_ids": [16],
                    }
                ]
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = asyncio.run(
            adapter.search_titles(
                query="Frieren",
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
        second = asyncio.run(
            adapter.search_titles(
                query="Frieren",
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert first == second
    assert first[0].localized_name == "葬送的芙莉莲"
    assert first[0].year == 2023
    assert len(requests) == 1
    assert requests[0].url.host == "api.themoviedb.org"
    assert requests[0].url.path == "/3/search/tv"
    assert requests[0].url.params["language"] == "zh-CN"


def test_series_and_season_parse_bounded_domain_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/tv/100":
            return _json_response(
                {
                    "id": 100,
                    "name": "动画",
                    "original_name": "Anime",
                    "poster_path": "/anime-poster.jpg",
                    "first_air_date": "2020-01-01",
                    "genres": [{"id": 16, "name": "Animation"}],
                    "seasons": [
                        {
                            "season_number": 0,
                            "episode_count": 2,
                            "name": "Specials",
                        }
                    ],
                }
            )
        return _json_response(
            {
                "season_number": 0,
                "episodes": [
                    {
                        "show_id": 100,
                        "season_number": 0,
                        "episode_number": 1,
                        "name": "OVA 1",
                        "overview": "",
                    },
                    {
                        "show_id": 100,
                        "season_number": 0,
                        "episode_number": 2,
                        "name": "随书附赠动画",
                        "overview": "",
                    },
                ]
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        series = asyncio.run(
            adapter.get_series(
                tmdb_id=100,
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
            )
        )
        season = asyncio.run(
            adapter.get_season(
                tmdb_id=100,
                work_type=TmdbWorkType.ANIME,
                season_number=0,
                language=TmdbLanguage.ZH_CN,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert series.first_air_year == 2020
    assert series.poster_path == "/anime-poster.jpg"
    assert series.seasons[0].season_number == 0
    assert tuple(
        episode.special_kind for episode in season.episodes
    ) == (SpecialKind.OVA, SpecialKind.OAD)


def test_series_rejects_untrusted_poster_url() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "id": 100,
                    "name": "动画",
                    "original_name": "Anime",
                    "first_air_date": "2020-01-01",
                    "genres": [{"id": 16}],
                    "poster_path": "https://attacker.invalid/poster.jpg",
                    "seasons": [],
                }
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_series(
                    tmdb_id=100,
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE


def test_timeout_is_retryable_and_does_not_disclose_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    adapter = TmdbHttpAdapter(
        api_key="secret-test-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.search_titles(
                    query="title",
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                    limit=10,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.UNAVAILABLE
    assert error.value.retryable
    assert error.value.__cause__ is None
    assert "secret-test-key" not in repr(adapter)
    assert "secret-test-key" not in str(error.value)


def test_malformed_content_encoding_is_an_invalid_response() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"not-gzip",
                headers={"content-encoding": "gzip"},
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.search_titles(
                    query="title",
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                    limit=10,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE
    assert not error.value.retryable
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "payload",
    (
        {"season_number": 1, "episodes": []},
        {
            "season_number": 0,
            "episodes": [
                {
                    "show_id": 999,
                    "season_number": 0,
                    "episode_number": 1,
                    "name": "OVA",
                    "overview": "",
                }
            ],
        },
    ),
)
def test_season_response_must_match_requested_series_and_season(
    payload: object,
) -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(payload)
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_season(
                    tmdb_id=100,
                    work_type=TmdbWorkType.ANIME,
                    season_number=0,
                    language=TmdbLanguage.ZH_CN,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE
    assert not error.value.retryable


def test_response_body_limit_fails_closed() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        limits=TmdbHttpLimits(max_response_bytes=1_024),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"x" * 1_025,
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.search_titles(
                    query="title",
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                    limit=10,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    (
        (401, TmdbErrorCode.AUTHENTICATION_FAILED, False),
        (404, TmdbErrorCode.NOT_FOUND, False),
        (429, TmdbErrorCode.RATE_LIMITED, True),
        (503, TmdbErrorCode.UNAVAILABLE, True),
    ),
)
def test_http_status_is_mapped_without_response_text(
    status: int,
    code: TmdbErrorCode,
    retryable: bool,
) -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                content=b"untrusted error body",
                request=request,
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_series(
                    tmdb_id=100,
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is code
    assert error.value.retryable is retryable


def test_movie_search_uses_movie_fields_and_reports_type() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(
            {
                "results": [
                    {
                        "id": 900,
                        "title": "千与千寻",
                        "original_title": "千と千尋の神隠し",
                        "release_date": "2001-07-20",
                        "original_language": "ja",
                        "genre_ids": [16, 14],
                    }
                ]
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="Spirited Away",
                work_type=TmdbWorkType.MOVIE,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert requests[0].url.path == "/3/search/movie"
    assert requests[0].url.params["include_adult"] == "true"
    assert results[0].localized_name == "千与千寻"
    assert results[0].year == 2001
    assert results[0].work_type is TmdbWorkType.MOVIE
    assert results[0].media_type.value == "movie"


def test_adult_movie_search_and_metadata_are_explicit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/3/search/movie":
            return _json_response(
                {
                    "results": [
                        {
                            "id": 1358188,
                            "title": "Adult fixture",
                            "original_title": "Adult fixture",
                            "release_date": "2024-09-06",
                            "original_language": "en",
                            "genre_ids": [18],
                            "adult": True,
                        }
                    ]
                }
            )
        return _json_response(
            {
                "adult": True,
                "genres": [{"id": 18, "name": "Drama"}],
                "id": 1358188,
                "original_language": "en",
                "original_title": "Adult fixture",
                "release_date": "2024-09-06",
                "title": "Adult fixture",
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="fixed adult fixture",
                work_type=TmdbWorkType.MOVIE,
                language=TmdbLanguage.EN_US,
                limit=10,
                include_adult=True,
            )
        )
        metadata = asyncio.run(
            adapter.get_movie(
                tmdb_id=1358188,
                work_type=TmdbWorkType.MOVIE,
                language=TmdbLanguage.EN_US,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert requests[0].url.params["include_adult"] == "true"
    assert requests[1].url.path == "/3/movie/1358188"
    assert results[0].tmdb_id == 1358188
    assert metadata.tmdb_id == 1358188
    assert metadata.adult is True
    assert metadata.release_year == 2024
    assert metadata.genre_ids == (18,)


def test_movie_metadata_rejects_non_boolean_adult_flag() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "adult": "true",
                    "genres": [],
                    "id": 1358188,
                    "original_language": "en",
                    "original_title": "Adult fixture",
                    "release_date": "2024-09-06",
                    "title": "Adult fixture",
                }
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_movie(
                    tmdb_id=1358188,
                    work_type=TmdbWorkType.MOVIE,
                    language=TmdbLanguage.EN_US,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "release_date",
    ("2024-02-30", "2024-not-a-date"),
)
def test_movie_metadata_rejects_invalid_release_date(
    release_date: str,
) -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "adult": False,
                    "genres": [],
                    "id": 1358188,
                    "original_language": "en",
                    "original_title": "Invalid date",
                    "release_date": release_date,
                    "title": "Invalid date",
                }
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_movie(
                    tmdb_id=1358188,
                    work_type=TmdbWorkType.MOVIE,
                    language=TmdbLanguage.EN_US,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE


def test_adult_search_option_requires_strict_boolean() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response({"results": []})

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.search_titles(
                    query="title",
                    work_type=TmdbWorkType.MOVIE,
                    language=TmdbLanguage.EN_US,
                    limit=10,
                    include_adult="true",  # type: ignore[arg-type]
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE
    assert request_count == 0


def test_anime_search_filters_explicit_non_animation_genres() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "results": [
                        {
                            "id": 10,
                            "name": "Live Action",
                            "original_name": "Live Action",
                            "first_air_date": "2020-01-01",
                            "original_language": "en",
                            "genre_ids": [18],
                        }
                    ]
                }
            )
        ),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="title",
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert results == ()


def test_anime_search_keeps_exact_title_when_genres_are_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/search/tv":
            return _json_response(
                {
                    "results": [
                        {
                            "id": 327371,
                            "name": "作弊道具管理局的工作EX",
                            "original_name": "チートアイテム管理局のお仕事EX",
                            "first_air_date": "2026-07-03",
                            "original_language": "ja",
                            "genre_ids": [],
                        }
                    ]
                }
            )
        return _json_response(
            {
                "id": 327371,
                "name": "作弊道具管理局的工作EX",
                "original_name": "チートアイテム管理局のお仕事EX",
                "first_air_date": "2026-07-03",
                "genres": [],
                "seasons": [
                    {
                        "season_number": 1,
                        "episode_count": 1,
                        "name": "第 1 季",
                    }
                ],
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="  チートアイテム管理局のお仕事ex  ",
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
        details = asyncio.run(
            adapter.get_series(
                tmdb_id=327371,
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert tuple(candidate.tmdb_id for candidate in results) == (327371,)
    assert details.tmdb_id == 327371
    assert details.seasons[0].episode_count == 1


def test_anime_search_hides_inexact_title_when_genres_are_missing() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "results": [
                        {
                            "id": 327371,
                            "name": "作弊道具管理局的工作EX",
                            "original_name": "チートアイテム管理局のお仕事EX",
                            "first_air_date": "2026-07-03",
                            "original_language": "ja",
                            "genre_ids": [],
                        }
                    ]
                }
            )
        ),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="お仕事",
                work_type=TmdbWorkType.ANIME,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert results == ()


def test_tv_series_search_keeps_non_animation_tv_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(
            {
                "results": [
                    {
                        "id": 20,
                        "name": "Live Action",
                        "original_name": "Live Action",
                        "first_air_date": "2021-01-01",
                        "original_language": "en",
                        "genre_ids": [18],
                    }
                ]
            }
        )

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        results = asyncio.run(
            adapter.search_titles(
                query="title",
                work_type=TmdbWorkType.TV_SERIES,
                language=TmdbLanguage.ZH_CN,
                limit=10,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert requests[0].url.path == "/3/search/tv"
    assert results[0].work_type is TmdbWorkType.TV_SERIES
    assert results[0].media_type.value == "tv"


def test_anime_series_details_revalidate_animation_genre() -> None:
    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(
            lambda request: _json_response(
                {
                    "id": 100,
                    "name": "Live Action",
                    "original_name": "Live Action",
                    "first_air_date": "2020-01-01",
                    "genres": [{"id": 18, "name": "Drama"}],
                    "seasons": [],
                }
            )
        ),
    )
    try:
        with pytest.raises(TmdbProviderError) as error:
            asyncio.run(
                adapter.get_series(
                    tmdb_id=100,
                    work_type=TmdbWorkType.ANIME,
                    language=TmdbLanguage.ZH_CN,
                )
            )
    finally:
        asyncio.run(adapter.aclose())

    assert error.value.code is TmdbErrorCode.INVALID_RESPONSE


def test_official_openapi_projected_contract_fixtures() -> None:
    fixture = json.loads(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        payload_by_path = {
            "/3/search/tv": fixture["tv_search"],
            "/3/search/movie": fixture["movie_search"],
            "/3/movie/11": fixture["movie_details"],
            "/3/tv/1399": fixture["tv_details"],
            "/3/tv/1399/season/1": fixture["season_details"],
        }
        return _json_response(payload_by_path[request.url.path])

    adapter = TmdbHttpAdapter(
        api_key="test-key-not-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        tv_results = asyncio.run(
            adapter.search_titles(
                query="Breaking Bad",
                work_type=TmdbWorkType.TV_SERIES,
                language=TmdbLanguage.EN_US,
                limit=10,
            )
        )
        movie_results = asyncio.run(
            adapter.search_titles(
                query="Fight Club",
                work_type=TmdbWorkType.MOVIE,
                language=TmdbLanguage.EN_US,
                limit=10,
            )
        )
        movie = asyncio.run(
            adapter.get_movie(
                tmdb_id=11,
                work_type=TmdbWorkType.MOVIE,
                language=TmdbLanguage.EN_US,
            )
        )
        series = asyncio.run(
            adapter.get_series(
                tmdb_id=1399,
                work_type=TmdbWorkType.TV_SERIES,
                language=TmdbLanguage.EN_US,
            )
        )
        season = asyncio.run(
            adapter.get_season(
                tmdb_id=1399,
                work_type=TmdbWorkType.TV_SERIES,
                season_number=1,
                language=TmdbLanguage.EN_US,
            )
        )
    finally:
        asyncio.run(adapter.aclose())

    assert tv_results[0].tmdb_id == 1396
    assert tv_results[0].year == 2008
    assert movie_results[0].tmdb_id == 550
    assert movie_results[1].year is None
    assert movie.tmdb_id == 11
    assert movie.adult is False
    assert movie.release_year == 1977
    assert tuple(
        summary.season_number for summary in series.seasons
    ) == (0, 1)
    assert season.tmdb_id == 1399
    assert season.season_number == 1
    assert season.episodes[0].episode_number == 1
