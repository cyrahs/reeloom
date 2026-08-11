from __future__ import annotations

import httpx

from reeloom.adapters.tmdb import TmdbClient


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
