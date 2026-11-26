"""Client for a real qBittorrent (the PT download client).

Not to be confused with ``server/qbittorrent.py``, which *serves* a
qBittorrent-compatible API over CloudDrive. This one drives the operator's
own qBittorrent: add a .torrent into a given folder, read progress, and
forget a torrent once its files have been archived. The address is the
operator's own client, set on the settings page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from reeloom.models import ReeloomError
from reeloom.redact import describe

_LOGGER = logging.getLogger(__name__)

#: States in which every byte is on disk.
COMPLETE_STATES = frozenset(
    {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
        "pausedUP",
        "stoppedUP",
        "checkingUP",
    }
)
ERROR_STATES = frozenset({"error", "missingFiles"})


class QbittorrentClientError(ReeloomError):
    pass


def _ok(response: httpx.Response) -> bool:
    """Any 2xx: qBittorrent 5.x answers several calls with 204 or a JSON
    body where older versions said 200 "Ok."."""

    return 200 <= response.status_code < 300


@dataclass(frozen=True, slots=True)
class TorrentStatus:
    info_hash: str
    name: str
    state: str
    progress: float
    amount_left: int
    save_path: str

    @property
    def complete(self) -> bool:
        return self.amount_left == 0 and (
            self.progress >= 1.0 or self.state in COMPLETE_STATES
        )

    @property
    def errored(self) -> bool:
        return self.state in ERROR_STATES


class QbittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise QbittorrentClientError("pt_client_invalid_url")
        self._base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self._username = username
        self.__password = password
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            # qBittorrent's CSRF check compares Referer/Origin with its host.
            headers={"Referer": self._base, "Origin": self._base},
        )
        self._logged_in = False
        # A rejected password is not retried until the settings change (which
        # builds a new client): qBittorrent bans an address after a few
        # failed logins, and the poll would walk straight into that.
        self._rejected = False

    def __repr__(self) -> str:
        return f"QbittorrentClient(base_url={self._base!r}, password=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise QbittorrentClientError(
                "pt_client_timeout", detail=describe(error, self.__password)
            ) from error
        except httpx.TransportError as error:
            raise QbittorrentClientError(
                "pt_client_unreachable", detail=describe(error, self.__password)
            ) from error

    async def login(self) -> None:
        if self._rejected:
            raise QbittorrentClientError("pt_client_login_failed")
        response = await self._send(
            "POST",
            "/api/v2/auth/login",
            data={"username": self._username, "password": self.__password},
        )
        if response.status_code == 403:
            # qBittorrent bans an address after repeated failures.
            raise QbittorrentClientError("pt_client_banned")
        # qBittorrent before 5.x answers 200 "Ok."; 5.x answers 204 with an
        # empty body (and a 401 or 200 "Fails." for a wrong password).
        accepted = response.status_code == 204 or (
            response.status_code == 200 and response.text.strip() == "Ok."
        )
        if not accepted:
            self._rejected = True
            raise QbittorrentClientError("pt_client_login_failed")
        self._logged_in = True

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._logged_in:
            await self.login()
        response = await self._send(method, path, **kwargs)
        if response.status_code == 403:
            # Session expired (or the client restarted): log in once more.
            self._logged_in = False
            await self.login()
            response = await self._send(method, path, **kwargs)
        return response

    async def version(self) -> str:
        response = await self._call("GET", "/api/v2/app/version")
        if response.status_code != 200:
            raise QbittorrentClientError("pt_client_error", status=response.status_code)
        return response.text.strip()[:40]

    async def ensure_category(self, category: str) -> None:
        response = await self._call(
            "POST", "/api/v2/torrents/createCategory", data={"category": category}
        )
        # 409: already exists, which is the point.
        if not (_ok(response) or response.status_code == 409):
            raise QbittorrentClientError(
                "pt_client_error", status=response.status_code, call="createCategory"
            )

    async def add_torrent(
        self,
        data: bytes,
        *,
        filename: str,
        save_path: str,
        download_path: str,
        category: str,
        tags: str,
    ) -> None:
        """Add a .torrent: in progress under ``download_path``, moved by
        qBittorrent to ``save_path`` once complete."""

        fields = {
            "savepath": save_path,
            "downloadPath": download_path,
            "useDownloadPath": "true",
            "category": category,
            "tags": tags,
            "autoTMM": "false",
            "contentLayout": "Original",
            "paused": "false",
            "stopped": "false",
        }
        response = await self._call(
            "POST",
            "/api/v2/torrents/add",
            data=fields,
            files={"torrents": (filename, data, "application/x-bittorrent")},
        )
        if response.status_code == 415:
            raise QbittorrentClientError("pt_client_invalid_torrent")
        if response.status_code == 409:
            raise QbittorrentClientError("pt_client_duplicate")
        if not _ok(response) or response.text.strip() == "Fails.":
            raise QbittorrentClientError(
                "pt_client_add_failed", status=response.status_code
            )

    async def torrents(self, hashes: list[str]) -> dict[str, TorrentStatus]:
        """Status by upper-case info hash; absent hashes are not in the client."""

        if not hashes:
            return {}
        response = await self._call(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": "|".join(item.lower() for item in hashes)},
        )
        if response.status_code != 200:
            raise QbittorrentClientError("pt_client_error", status=response.status_code)
        payload = response.json()
        if not isinstance(payload, list):
            raise QbittorrentClientError("pt_client_malformed")
        result: dict[str, TorrentStatus] = {}
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("hash"), str):
                continue
            status = TorrentStatus(
                info_hash=item["hash"].upper(),
                name=str(item.get("name") or ""),
                state=str(item.get("state") or ""),
                progress=float(item.get("progress") or 0.0),
                amount_left=int(item.get("amount_left") or 0),
                save_path=str(item.get("save_path") or ""),
            )
            result[status.info_hash] = status
        return result

    async def delete(self, info_hash: str, *, delete_files: bool) -> None:
        response = await self._call(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": info_hash.lower(),
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        if not _ok(response):
            raise QbittorrentClientError(
                "pt_client_error", status=response.status_code, call="delete"
            )
