"""TMDB read-only client.

Bounded and fixed-origin: the key is injected, the host is not configurable,
responses are size-capped, and only the five endpoints the Agent needs are
reachable. Everything it returns is untrusted text that must pass through
``sanitize_title`` before touching a path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from reeloom.models import ReeloomError

_ORIGIN = "https://api.themoviedb.org/3"
_LANGUAGE = "zh-CN"
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_RESULTS = 8
_MAX_OVERVIEW = 300

_LOGGER = logging.getLogger(__name__)


class TmdbError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class TmdbHit:
    tmdb_id: int
    title: str
    original_title: str
    year: int | None
    overview: str

    def to_json(self) -> dict[str, Any]:
        return {
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "overview": self.overview,
        }


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise TmdbError("missing_tmdb_key")
        self.__api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}

    def __repr__(self) -> str:
        return "TmdbClient(api_key=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: str) -> dict[str, Any]:
        key = (path, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        try:
            response = await self._client.get(
                path, params={**params, "api_key": self.__api_key}
            )
        except httpx.TimeoutException as error:
            raise TmdbError("tmdb_timeout", path=path) from error
        except httpx.TransportError as error:
            raise TmdbError("tmdb_unreachable", path=path) from error

        if response.status_code == 404:
            raise TmdbError("tmdb_not_found", path=path)
        if response.status_code == 401:
            raise TmdbError("tmdb_unauthorized")
        if response.status_code == 429:
            raise TmdbError("tmdb_rate_limited")
        if response.status_code >= 400:
            raise TmdbError("tmdb_error", status=response.status_code)
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise TmdbError("tmdb_response_too_large", path=path)

        payload = response.json()
        if not isinstance(payload, dict):
            raise TmdbError("tmdb_malformed", path=path)
        self._cache[key] = payload
        return payload

    async def search(self, query: str, *, movie: bool) -> list[TmdbHit]:
        if not query.strip():
            raise TmdbError("empty_query")
        path = "/search/movie" if movie else "/search/tv"
        payload = await self._get(
            path,
            query=query[:200],
            language=_LANGUAGE,
            include_adult="true",
        )
        results = payload.get("results") or []
        hits: list[TmdbHit] = []
        for item in results[:_MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            hits.append(
                TmdbHit(
                    tmdb_id=int(item.get("id", 0)),
                    title=str(item.get("title") or item.get("name") or ""),
                    original_title=str(
                        item.get("original_title")
                        or item.get("original_name")
                        or ""
                    ),
                    year=_year(
                        item.get("release_date") or item.get("first_air_date")
                    ),
                    overview=str(item.get("overview") or "")[:_MAX_OVERVIEW],
                )
            )
        return [hit for hit in hits if hit.tmdb_id > 0]

    async def get_series(self, tmdb_id: int) -> dict[str, Any]:
        payload = await self._get(f"/tv/{int(tmdb_id)}", language=_LANGUAGE)
        seasons = [
            {
                "season": int(item.get("season_number", 0)),
                "name": str(item.get("name") or ""),
                "episode_count": int(item.get("episode_count", 0)),
                "air_year": _year(item.get("air_date")),
            }
            for item in payload.get("seasons") or []
            if isinstance(item, dict)
        ]
        return {
            "tmdb_id": int(payload.get("id", tmdb_id)),
            "title": str(payload.get("name") or ""),
            "original_title": str(payload.get("original_name") or ""),
            "year": _year(payload.get("first_air_date")),
            "overview": str(payload.get("overview") or "")[:_MAX_OVERVIEW],
            "seasons": seasons,
        }

    async def get_season(self, tmdb_id: int, season: int) -> dict[str, Any]:
        payload = await self._get(
            f"/tv/{int(tmdb_id)}/season/{int(season)}", language=_LANGUAGE
        )
        episodes = [
            {
                "episode": int(item.get("episode_number", 0)),
                "name": str(item.get("name") or "")[:120],
                "air_date": str(item.get("air_date") or ""),
            }
            for item in payload.get("episodes") or []
            if isinstance(item, dict)
        ]
        return {
            "tmdb_id": int(tmdb_id),
            "season": int(season),
            "name": str(payload.get("name") or ""),
            "episodes": episodes,
        }

    async def get_movie(self, tmdb_id: int) -> dict[str, Any]:
        payload = await self._get(f"/movie/{int(tmdb_id)}", language=_LANGUAGE)
        return {
            "tmdb_id": int(payload.get("id", tmdb_id)),
            "title": str(payload.get("title") or ""),
            "original_title": str(payload.get("original_title") or ""),
            "year": _year(payload.get("release_date")),
            "runtime": payload.get("runtime"),
            "overview": str(payload.get("overview") or "")[:_MAX_OVERVIEW],
        }


def _year(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        return None
    year = int(value[:4])
    return year if 1000 <= year <= 9999 else None
