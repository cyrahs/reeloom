from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from agents import Model, ModelSettings
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from reeloom.server.config import ProviderConfig
from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_MODEL_RESPONSE_BYTES = 8 * 1024 * 1024


def _origin(value: str, *, allow_path: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ServerError(
            ServerErrorCode.PROVIDER_ORIGIN_REJECTED
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
    ):
        raise ServerError(ServerErrorCode.PROVIDER_ORIGIN_REJECTED)
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if port is None or port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


@dataclass(frozen=True, slots=True)
class ProviderOriginPolicy:
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        origins: tuple[str, ...],
    ) -> ProviderOriginPolicy:
        if (
            not isinstance(origins, tuple)
            or not origins
            or len(origins) > 32
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        normalized = frozenset(
            _origin(value, allow_path=False) for value in origins
        )
        if len(normalized) != len(origins):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        return cls(normalized)

    def validate_base_url(self, value: str) -> str:
        normalized = _origin(value, allow_path=True)
        if normalized not in self.allowed_origins:
            raise ServerError(
                ServerErrorCode.PROVIDER_ORIGIN_REJECTED
            )
        return value


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    available: bool
    status_code: int


class ProviderProbe(Protocol):
    async def probe(
        self,
        *,
        config: ProviderConfig,
        api_key: bytes,
    ) -> ProviderProbeResult: ...


class _PinnedOriginTransport(httpx.AsyncBaseTransport):
    """Resolve once, connect to that address, preserve TLS SNI and Host."""

    def __init__(
        self,
        base_url: str,
        *,
        resolver: object = socket.getaddrinfo,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = _MAX_MODEL_RESPONSE_BYTES,
        total_timeout_seconds: float = 60.0,
    ) -> None:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        if host is None:
            raise ServerError(ServerErrorCode.PROVIDER_ORIGIN_REJECTED)
        port = parsed.port or 443
        try:
            rows = resolver(host, port, type=socket.SOCK_STREAM)
            addresses = sorted(
                {
                    str(ipaddress.ip_address(row[4][0]))
                    for row in rows
                }
            )
        except Exception:
            raise ServerError(ServerErrorCode.PROVIDER_UNAVAILABLE) from None
        if not addresses:
            raise ServerError(ServerErrorCode.PROVIDER_UNAVAILABLE)
        if (
            type(max_response_bytes) is not int
            or max_response_bytes < 1
            or not isinstance(total_timeout_seconds, (int, float))
            or isinstance(total_timeout_seconds, bool)
            or total_timeout_seconds <= 0
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        self._origin = _origin(base_url, allow_path=True)
        self._hostname = host
        self._port = port
        self._address = addresses[0]
        self._max_response_bytes = max_response_bytes
        self._total_timeout = float(total_timeout_seconds)
        self._transport = transport or httpx.AsyncHTTPTransport(
            retries=0
        )

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        request_origin = _origin(str(request.url), allow_path=True)
        if request_origin != self._origin:
            raise ServerError(ServerErrorCode.PROVIDER_ORIGIN_REJECTED)
        port_text = "" if self._port == 443 else f":{self._port}"
        pinned_url = request.url.copy_with(
            host=self._address,
            port=None if self._port == 443 else self._port,
        )
        headers = request.headers.copy()
        headers["host"] = f"{self._hostname}{port_text}"
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self._hostname
        pinned_request = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_timeout
        try:
            async with asyncio.timeout_at(deadline):
                response = await self._transport.handle_async_request(
                    pinned_request
                )
        except TimeoutError:
            raise httpx.ReadTimeout(
                "provider total deadline exceeded",
                request=pinned_request,
            ) from None
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_BoundedResponseStream(
                response.stream,
                request=pinned_request,
                max_bytes=self._max_response_bytes,
                deadline=deadline,
            ),
            extensions=response.extensions,
            request=pinned_request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class _BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        request: httpx.Request,
        max_bytes: int,
        deadline: float,
    ) -> None:
        self._stream = stream
        self._request = request
        self._max_bytes = max_bytes
        self._deadline = deadline

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        try:
            async with asyncio.timeout_at(self._deadline):
                async for chunk in self._stream:
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise httpx.DecodingError(
                            "provider response exceeds limit",
                            request=self._request,
                        )
                    yield chunk
        except TimeoutError:
            raise httpx.ReadTimeout(
                "provider total deadline exceeded",
                request=self._request,
            ) from None

    async def aclose(self) -> None:
        await self._stream.aclose()


class ControlledProviderProbe:
    def __init__(
        self,
        *,
        origins: ProviderOriginPolicy,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._origins = origins
        self._timeout = timeout_seconds

    async def probe(
        self,
        *,
        config: ProviderConfig,
        api_key: bytes,
    ) -> ProviderProbeResult:
        self._origins.validate_base_url(config.base_url)
        if (
            not isinstance(api_key, bytes)
            or not 0 < len(api_key) <= 4_096
        ):
            raise ServerError(ServerErrorCode.INVALID_SECRET)
        transport = _PinnedOriginTransport(
            config.base_url,
            max_response_bytes=64 * 1024,
            total_timeout_seconds=self._timeout,
        )
        try:
            async with httpx.AsyncClient(
                base_url=config.base_url.rstrip("/") + "/",
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "authorization": (
                        "Bearer " + api_key.decode("utf-8", errors="strict")
                    ),
                    "accept": "application/json",
                },
            ) as client:
                async with client.stream("GET", "models") as response:
                    await response.aread()
                    status = response.status_code
                    return ProviderProbeResult(
                        available=200 <= status < 300,
                        status_code=status,
                    )
        except (httpx.HTTPError, UnicodeError):
            raise ServerError(ServerErrorCode.PROVIDER_UNAVAILABLE) from None


class ModelLease(Protocol):
    @property
    def model(self) -> Model: ...

    @property
    def model_settings(self) -> ModelSettings: ...

    async def close(self) -> None: ...


class ControlledModelLease:
    """One explicitly configured model client with a pinned provider origin."""

    def __init__(
        self,
        *,
        origins: ProviderOriginPolicy,
        config: ProviderConfig,
        api_key: bytes,
        timeout_seconds: float = 60.0,
    ) -> None:
        origins.validate_base_url(config.base_url)
        if (
            not isinstance(api_key, bytes)
            or not 0 < len(api_key) <= 4_096
        ):
            raise ServerError(ServerErrorCode.INVALID_SECRET)
        try:
            credential = api_key.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ServerError(ServerErrorCode.INVALID_SECRET) from None
        if not credential or any(character.isspace() for character in credential):
            raise ServerError(ServerErrorCode.INVALID_SECRET)
        transport = _PinnedOriginTransport(
            config.base_url,
            max_response_bytes=_MAX_MODEL_RESPONSE_BYTES,
            total_timeout_seconds=timeout_seconds,
        )
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=credential,
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
            base_url=config.base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            max_retries=0,
            http_client=self._http,
        )
        self._model: Model = OpenAIResponsesModel(
            model=config.model,
            openai_client=self._client,
        )
        self._settings = ModelSettings(
            reasoning=(
                Reasoning(effort=config.reasoning_effort)
                if config.reasoning_effort is not None
                else None
            ),
            verbosity=config.verbosity,
        )
        self._closed = False

    @property
    def model(self) -> Model:
        return self._model

    @property
    def model_settings(self) -> ModelSettings:
        return self._settings

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()
