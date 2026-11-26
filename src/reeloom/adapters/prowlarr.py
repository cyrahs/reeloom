"""Prowlarr search client: the subscription's only way to an indexer.

Reeloom never talks to a tracker itself. Prowlarr holds the indexer
credentials (a private tracker's API key never reaches this process), applies
the indexer's own query and grab limits, and proxies the .torrent download.
The address is the operator's own Prowlarr, set on the settings page; the
only URLs ever requested are its search API and the download links it hands
out, re-anchored onto that configured address.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from reeloom.models import ReeloomError
from reeloom.redact import describe

_LOGGER = logging.getLogger(__name__)

MAX_RESULTS = 100
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TORRENT_BYTES = 8 * 1024 * 1024


class ProwlarrError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class Release:
    guid: str
    title: str
    size_bytes: int
    indexer_id: int
    indexer: str
    download_url: str
    info_url: str
    published_at: datetime | None
    seeders: int | None
    flags: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Everything but the download link, which carries Prowlarr's key."""

        return {
            "guid": self.guid,
            "title": self.title,
            "size_bytes": self.size_bytes,
            "indexer_id": self.indexer_id,
            "indexer": self.indexer,
            "info_url": self.info_url,
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
            "seeders": self.seeders,
            "flags": list(self.flags),
        }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _release(item: Any) -> Release | None:
    if not isinstance(item, dict):
        return None
    guid = item.get("guid")
    title = item.get("title")
    download = item.get("downloadUrl")
    if not (isinstance(guid, str) and isinstance(title, str) and guid and title):
        return None
    if not isinstance(download, str) or not download:
        # Magnet-only results have no downloadUrl; private trackers always do.
        return None
    flags = item.get("indexerFlags") or []
    seeders = item.get("seeders")
    return Release(
        guid=guid[:500],
        title=title[:500],
        size_bytes=int(item.get("size") or 0),
        indexer_id=int(item.get("indexerId") or 0),
        indexer=str(item.get("indexer") or "")[:100],
        download_url=download,
        info_url=str(item.get("infoUrl") or "")[:500],
        published_at=_parse_time(item.get("publishDate")),
        seeders=int(seeders) if isinstance(seeders, int) else None,
        flags=tuple(str(flag)[:40] for flag in flags if isinstance(flag, str))[:10],
    )


class ProwlarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise ProwlarrError("prowlarr_not_configured")
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ProwlarrError("prowlarr_invalid_url")
        self._base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self.__api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def __repr__(self) -> str:
        return f"ProwlarrClient(base_url={self._base!r}, api_key=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.get(url, **kwargs)
        except httpx.TimeoutException as error:
            raise ProwlarrError(
                "prowlarr_timeout", detail=describe(error, self.__api_key)
            ) from error
        except httpx.TransportError as error:
            raise ProwlarrError(
                "prowlarr_unreachable", detail=describe(error, self.__api_key)
            ) from error

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise ProwlarrError("prowlarr_unauthorized")
        if response.status_code == 429:
            raise ProwlarrError("prowlarr_rate_limited")
        if response.status_code >= 400:
            raise ProwlarrError("prowlarr_error", status=response.status_code)

    async def indexers(self) -> list[dict[str, Any]]:
        """Enabled indexers as ``{id, name, protocol, privacy}``."""

        response = await self._get(f"{self._base}/api/v1/indexer")
        self._check(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise ProwlarrError("prowlarr_malformed")
        return [
            {
                "id": item.get("id"),
                "name": str(item.get("name") or ""),
                "protocol": str(item.get("protocol") or ""),
                "privacy": str(item.get("privacy") or ""),
                "enabled": bool(item.get("enable")),
            }
            for item in payload
            if isinstance(item, dict)
        ]

    async def search(self, query: str, indexer_ids: tuple[int, ...]) -> list[Release]:
        params: list[tuple[str, str]] = [
            ("query", query),
            ("type", "search"),
            ("limit", str(MAX_RESULTS)),
        ]
        params += [("indexerIds", str(value)) for value in indexer_ids]
        response = await self._get(f"{self._base}/api/v1/search", params=params)
        self._check(response)
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProwlarrError("prowlarr_response_too_large")
        payload = response.json()
        if not isinstance(payload, list):
            raise ProwlarrError("prowlarr_malformed")
        releases = [release for item in payload if (release := _release(item))]
        if indexer_ids:
            wanted = set(indexer_ids)
            releases = [item for item in releases if item.indexer_id in wanted]
        return releases

    def _download_target(self, download_url: str) -> str:
        """Re-anchor a download link onto the configured Prowlarr.

        Prowlarr builds links from the host it believes it has (often
        127.0.0.1), so only the path and query are kept. Anything that is not
        a Prowlarr download path is refused rather than fetched.
        """

        parts = urlsplit(download_url)
        segments = [segment for segment in parts.path.split("/") if segment]
        if len(segments) < 2 or segments[-1] != "download":
            raise ProwlarrError("prowlarr_unexpected_link")
        base_path = urlsplit(self._base).path.rstrip("/")
        path = parts.path
        if base_path and path.startswith(base_path + "/"):
            path = path[len(base_path) :]
        query = f"?{parts.query}" if parts.query else ""
        return f"{self._base}{path}{query}"

    async def fetch_torrent(self, download_url: str) -> bytes:
        response = await self._get(self._download_target(download_url))
        if response.is_redirect:
            # A magnet redirect: not what a private tracker hands out, and
            # following arbitrary redirects is not allowed.
            raise ProwlarrError("prowlarr_redirected_download")
        self._check(response)
        content = response.content
        if len(content) > MAX_TORRENT_BYTES:
            raise ProwlarrError("torrent_too_large")
        if not content.startswith(b"d"):
            raise ProwlarrError("prowlarr_not_a_torrent")
        return content
