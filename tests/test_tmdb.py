from __future__ import annotations

import httpx
import pytest

from reeloom.adapters.tmdb import TmdbClient, TmdbError


def client_returning(payload: dict) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return TmdbClient("key", transport=httpx.MockTransport(handler))


async def test_poster_url_is_built_on_the_fixed_image_origin() -> None:
    client = client_returning({"id": 123, "poster_path": "/abc_123-x.jpg"})
    url = await client.poster_url(123, movie=False)
    assert url == "https://image.tmdb.org/t/p/w780/abc_123-x.jpg"
    await client.aclose()


async def test_poster_url_is_none_when_the_work_has_no_poster() -> None:
    client = client_returning({"id": 123, "poster_path": None})
    assert await client.poster_url(123, movie=True) is None
    await client.aclose()


async def test_a_malformed_poster_path_is_dropped_not_used() -> None:
    for path in ("../etc/passwd", "/a/b.jpg", "/x.png", "//evil.com/p.jpg"):
        client = client_returning({"id": 123, "poster_path": path})
        assert await client.poster_url(123, movie=True) is None
        await client.aclose()


def client_capturing(payload: dict, requests: list[httpx.Request]) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    return TmdbClient("key", transport=httpx.MockTransport(handler))


async def test_search_returns_a_whole_page_with_adult_flag_and_dates() -> None:
    # A title shared by ten mainstream dramas and, further down the page,
    # the adult OVA the folder is actually about. With an 8-hit cap the
    # right entry never reached the Agent.
    results = [
        {"id": i, "name": f"告白 {i}", "first_air_date": "2020-01-01"}
        for i in range(1, 10)
    ]
    results.append(
        {
            "id": 222931,
            "name": "告白……",
            "original_name": "告白……",
            "first_air_date": "2023-04-28",
            "adult": True,
        }
    )
    results += [{"id": i, "name": f"告白 {i}"} for i in range(11, 21)]
    requests: list[httpx.Request] = []
    client = client_capturing(
        {"page": 1, "total_pages": 3, "results": results}, requests
    )

    found = await client.search("告白", movie=False)

    assert (found.page, found.total_pages) == (1, 3)
    assert len(found.hits) == 20
    hit = next(hit for hit in found.hits if hit.tmdb_id == 222931)
    assert (hit.adult, hit.date, hit.year) == (True, "2023-04-28", 2023)
    assert found.hits[0].adult is False
    assert found.hits[-1].date == ""
    assert requests[0].url.params["page"] == "1"
    assert requests[0].url.params["include_adult"] == "true"
    await client.aclose()


async def test_search_forwards_the_page_and_clamps_total_pages() -> None:
    requests: list[httpx.Request] = []
    client = client_capturing({"page": 3, "total_pages": 40, "results": []}, requests)

    found = await client.search("x", movie=True, page=3)

    assert requests[0].url.params["page"] == "3"
    assert (found.page, found.total_pages, found.hits) == (3, 5, ())
    await client.aclose()


async def test_search_rejects_pages_outside_the_allowed_range() -> None:
    client = client_returning({"results": []})
    for page in (0, 6):
        with pytest.raises(TmdbError) as error:
            await client.search("x", movie=False, page=page)
        assert error.value.code == "invalid_page"
    await client.aclose()


async def test_series_details_carry_adult_flag_and_air_dates() -> None:
    client = client_returning(
        {
            "id": 222931,
            "name": "告白……",
            "first_air_date": "2023-04-28",
            "last_air_date": "2026-08-28",
            "adult": True,
            "seasons": [
                {"season_number": 1, "episode_count": 4, "air_date": "2023-04-28"}
            ],
        }
    )

    details = await client.get_series(222931)

    assert details["adult"] is True
    assert (details["first_air_date"], details["last_air_date"]) == (
        "2023-04-28",
        "2026-08-28",
    )
    assert details["seasons"][0]["air_date"] == "2023-04-28"
    await client.aclose()


async def test_movie_details_carry_adult_flag_and_release_date() -> None:
    client = client_returning(
        {"id": 7, "title": "Feature", "release_date": "2016-05-01", "adult": False}
    )

    details = await client.get_movie(7)

    assert (details["adult"], details["release_date"]) == (False, "2016-05-01")
    await client.aclose()
