from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reeloom.executor.apply import ApplyResult
from reeloom.executor.folder_disposition import FolderDispositionResult
from reeloom.server.auth import AuthSettings
from reeloom.server.interactions import (
    InteractionKind,
    InteractionService,
)
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.api_models import (
    ApplyResponse,
    ApproveApplyRequest,
    ConfigResponse,
    ConfigUpdateRequest,
    DirectoryListingResponse,
    DiscoveriesResponse,
    FolderObservationsResponse,
    EventsResponse,
    FolderDispositionRecoveryRequest,
    FolderDispositionRequest,
    FolderDispositionResultResponse,
    HealthResponse,
    InteractionHistoryResponse,
    InteractionRequest,
    InteractionResponse,
    MoveCapabilityProbeRequest,
    MoveCapabilityResponse,
    PlanLineageResponse,
    PlanLineageItem,
    PlanPreviewResponse,
    ProviderProbeRequest,
    ProviderProbeResponse,
    ReapplyRequest,
    ReapplyResponse,
    RecoveryRequest,
    RecoveryResponse,
    RunDeletionResponse,
    RunResponse,
    RunsResponse,
    SessionResponse,
    SubtitleAcquisitionApprovalRequest,
    SubtitleAcquisitionResponse,
    TelegramTestRequest,
    TelegramTestResponse,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.folder_disposition import FolderDispositionCoordinator
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
    SubtitleAcquisitionRequestRecord,
)
from reeloom.server.web_static import StaticAsset, StaticWebBundle
from reeloom.executor.errors import (
    ApprovalError,
    ExecutorError,
    ExecutorErrorCode,
)

_MAX_BODY_BYTES = 64 * 1024
_MAX_PAGE_SIZE = 100
_BODY_TIMEOUT_SECONDS = 5.0
_EMPTY_SSE_POLLS = 2
_SSE_POLL_SECONDS = 0.25
_SSE_CONNECTION_LIMIT = 16
_SINGLETON_HEADERS = frozenset(
    {
        "authorization",
        "content-length",
        "content-type",
        "host",
        "idempotency-key",
        "if-match",
        "last-event-id",
        "origin",
    }
)


def _folder_disposition_payload(
    result: FolderDispositionResult,
) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "plan_hash": result.plan_hash,
        "approval_id": result.approval_id,
        "transaction_id": result.transaction_id,
        "action": result.action.value,
        "target_relative": result.target_relative,
        "status": result.status,
    }


class ApiQueries(Protocol):
    def get_run(self, run_id: str) -> dict[str, object] | None: ...

    def is_run_visible(self, run_id: str) -> bool: ...

    def list_events(
        self,
        *,
        run_id: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[dict[str, object], ...]: ...

    def latest_event_id(self, run_id: str) -> int: ...

    def get_config(self) -> dict[str, object] | None: ...

    def get_plan(
        self,
        *,
        run_id: str,
        version: int | None,
    ) -> dict[str, object] | None: ...

    def list_runs(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]: ...

    def list_discoveries(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]: ...

    def list_folder_observations(
        self, *, limit: int
    ) -> tuple[dict[str, object], ...]: ...

    def list_plans(
        self,
        *,
        run_id: str,
        before_version: int | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]: ...

    def get_plan_preview(
        self,
        *,
        run_id: str,
        version: int,
        after: int,
        limit: int,
    ) -> dict[str, object] | None: ...

    def list_interactions(
        self,
        *,
        run_id: str,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]: ...


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    queries: ApiQueries
    interactions: InteractionService | None = None
    apply: ApplyCoordinator | None = None
    folder_dispositions: FolderDispositionCoordinator | None = None
    subtitle_acquisitions: SubtitleAcquisitionCoordinator | None = None
    health: Callable[[], object] | None = None
    config_update: (
        Callable[[int, dict[str, object]], dict[str, object]] | None
    ) = None
    config_resolve: (
        Callable[[int, dict[str, object]], dict[str, object] | None]
        | None
    ) = None
    provider_probe: Callable[[], Awaitable[object]] | None = None
    telegram_test: Callable[[str], dict[str, object]] | None = None
    move_capability_probe: (
        Callable[[str], Awaitable[dict[str, object]]] | None
    ) = None
    directory_list: (
        Callable[[str], dict[str, object]] | None
    ) = None
    idempotency: object | None = None
    run_delete: (
        Callable[[str], dict[str, object]] | None
    ) = None
    run_delete_resolve: (
        Callable[[str], dict[str, object] | None] | None
    ) = None
    sse_max_empty_polls: int | None = _EMPTY_SSE_POLLS
    sse_poll_seconds: float = _SSE_POLL_SECONDS
    sse_heartbeat_seconds: float = 15.0


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _host_name(value: str) -> str | None:
    try:
        parsed = urlsplit(f"//{value}")
        parsed.port
    except ValueError:
        return None
    if (
        not value
        or value.endswith(":")
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.lower()


class _SecurityBoundary:
    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: AuthSettings,
        public_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self._auth = auth
        self._public_paths = public_paths
        self._concurrency = threading.BoundedSemaphore(64)
        self._sse_concurrency = threading.BoundedSemaphore(
            _SSE_CONNECTION_LIMIT
        )
        self._rate_lock = threading.Lock()
        self._arrivals: deque[float] = deque()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        concurrency = self._concurrency_gate(scope["path"])
        if not concurrency.acquire(blocking=False):
            await JSONResponse(
                {"error": {"code": "server_busy"}},
                status_code=503,
            )(scope, receive, send)
            return
        try:
            if not self._allow_arrival():
                await JSONResponse(
                    {"error": {"code": "rate_limited"}},
                    status_code=429,
                )(scope, receive, send)
                return
            header_pairs = tuple(
                (
                    key.decode("latin-1").lower(),
                    value.decode("latin-1"),
                )
                for key, value in scope["headers"]
            )
            if any(
                sum(1 for key, _ in header_pairs if key == name) > 1
                for name in _SINGLETON_HEADERS
            ):
                await JSONResponse(
                    {"error": {"code": "duplicate_header"}},
                    status_code=400,
                )(scope, receive, send)
                return
            headers = dict(header_pairs)
            host = _host_name(headers.get("host", ""))
            if host not in self._auth.allowed_hosts:
                await JSONResponse(
                    {"error": {"code": "invalid_host"}},
                    status_code=400,
                )(scope, receive, send)
                return
            origin = headers.get("origin")
            if origin is not None and origin not in self._auth.allowed_origins:
                await JSONResponse(
                    {"error": {"code": "invalid_origin"}},
                    status_code=403,
                )(scope, receive, send)
                return
            if scope["method"] == "OPTIONS":
                await self._send_with_headers(
                    Response(status_code=204),
                    scope,
                    receive,
                    send,
                    origin,
                )
                return
            if (
                scope["method"] in {"GET", "HEAD"}
                and scope["path"] in self._public_paths
            ):
                async def public_send(message: Message) -> None:
                    if message["type"] == "http.response.start":
                        message["headers"] = list(
                            message.get("headers", [])
                        ) + self._headers(origin)
                    await send(message)

                await self.app(scope, receive, public_send)
                return
            authorization = headers.get("authorization", "")
            prefix = "Bearer "
            authenticated = (
                self._auth.authenticate(authorization[len(prefix) :])
                if authorization.startswith(prefix)
                else False
            )
            if not authenticated:
                await self._send_with_headers(
                    JSONResponse(
                        {"error": {"code": "unauthorized"}},
                        status_code=401,
                        headers={"www-authenticate": "Bearer"},
                    ),
                    scope,
                    receive,
                    send,
                    origin,
                )
                return
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                invalid, receive = await self._validate_json(
                    headers,
                    receive,
                )
                if invalid is not None:
                    await self._send_with_headers(
                        invalid,
                        scope,
                        receive,
                        send,
                        origin,
                    )
                    return

            async def send_with_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    extra = self._headers(origin)
                    message["headers"] = list(message.get("headers", [])) + extra
                await send(message)

            await self.app(scope, receive, send_with_headers)
        finally:
            concurrency.release()

    def _concurrency_gate(
        self,
        path: str,
    ) -> threading.BoundedSemaphore:
        if path.startswith("/api/v1/runs/") and path.endswith(
            "/events/stream"
        ):
            return self._sse_concurrency
        return self._concurrency

    def _allow_arrival(self) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            while self._arrivals and self._arrivals[0] <= now - 60:
                self._arrivals.popleft()
            if len(self._arrivals) >= 10_000:
                return False
            self._arrivals.append(now)
            return True

    async def _validate_json(
        self,
        headers: dict[str, str],
        receive: Receive,
    ) -> tuple[JSONResponse | None, Receive]:
        content_type = headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/json":
            return JSONResponse(
                {"error": {"code": "invalid_content_type"}},
                status_code=415,
            ), receive
        length = headers.get("content-length")
        if length is not None:
            try:
                if int(length) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        {"error": {"code": "body_too_large"}},
                        status_code=413,
                    ), receive
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_body"}},
                    status_code=400,
                ), receive
        chunks: list[bytes] = []
        size = 0
        more = True
        try:
            async with asyncio.timeout(_BODY_TIMEOUT_SECONDS):
                while more:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return JSONResponse(
                            {"error": {"code": "invalid_body"}},
                            status_code=400,
                        ), receive
                    chunk = message.get("body", b"")
                    chunks.append(chunk)
                    size += len(chunk)
                    more = bool(message.get("more_body", False))
                    if size > _MAX_BODY_BYTES:
                        return JSONResponse(
                            {"error": {"code": "body_too_large"}},
                            status_code=413,
                        ), receive
        except TimeoutError:
            return JSONResponse(
                {"error": {"code": "request_timeout"}},
                status_code=408,
            ), receive
        body = b"".join(chunks)
        if len(body) > _MAX_BODY_BYTES:
            return JSONResponse(
                {"error": {"code": "body_too_large"}},
                status_code=413,
            ), receive
        try:
            json.loads(
                body,
                object_pairs_hook=_duplicate_rejecting_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": {"code": "invalid_json"}},
                status_code=400,
            ), receive
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        return None, replay

    def _headers(self, origin: str | None) -> list[tuple[bytes, bytes]]:
        result = [
            (b"x-content-type-options", b"nosniff"),
            (b"cache-control", b"no-store"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (
                b"content-security-policy",
                (
                    b"default-src 'none'; base-uri 'none'; "
                    b"connect-src 'self'; font-src 'self'; "
                    b"form-action 'self'; frame-ancestors 'none'; "
                    b"img-src 'self' data:; object-src 'none'; "
                    b"script-src 'self'; style-src 'self'"
                ),
            ),
            (
                b"permissions-policy",
                (
                    b"camera=(), geolocation=(), microphone=(), "
                    b"payment=(), usb=()"
                ),
            ),
        ]
        if origin is not None:
            result.extend(
                [
                    (b"access-control-allow-origin", origin.encode("ascii")),
                    (b"vary", b"Origin"),
                    (
                        b"access-control-allow-headers",
                        b"Authorization, Content-Type, Idempotency-Key, If-Match, Last-Event-ID",
                    ),
                    (
                        b"access-control-allow-methods",
                        b"GET, POST, PUT, DELETE, OPTIONS",
                    ),
                ]
            )
        return result

    async def _send_with_headers(
        self,
        response: Response,
        scope: Scope,
        receive: Receive,
        send: Send,
        origin: str | None,
    ) -> None:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + (
                    self._headers(origin)
                )
            await send(message)

        await response(scope, receive, wrapped)


def _cursor(
    raw: Annotated[
        str | None,
        Header(
            alias="Last-Event-ID",
            description="Durable event cursor",
        ),
    ] = None,
) -> int:
    raw = "0" if raw is None else raw
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor"},
        ) from None
    if value < 0:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor"},
        )
    return value


def _idempotency_key(
    value: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Stable key for one logical mutation",
        ),
    ] = None,
) -> str:
    value = "" if value is None else value
    if not value or len(value.encode("utf-8")) > 256:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return value


def _plan_hash(
    value: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description="Exact immutable plan hash",
        ),
    ] = None,
) -> str:
    value = "" if value is None else value
    digest = value.removeprefix("sha256:")
    if (
        len(value) != 71
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_plan_hash"},
        )
    return value


def _config_revision(
    value: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description="Exact config revision",
        ),
    ] = None,
) -> int:
    try:
        return int("" if value is None else value)
    except ValueError:
        raise HTTPException(
            400, detail={"code": "invalid_revision"}
        ) from None


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(400, detail={"code": "invalid_body"})
    return value


async def _shield_thread(function: Callable[[], object]) -> object:
    task = asyncio.create_task(asyncio.to_thread(function))
    return await asyncio.shield(task)


def create_api(
    dependencies: ApiDependencies,
    *,
    auth: AuthSettings,
    static_root: Path | None = None,
) -> FastAPI:
    static_bundle = (
        None if static_root is None else StaticWebBundle.load(static_root)
    )
    app = FastAPI(
        title="Reeloom API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.add_middleware(
        _SecurityBoundary,
        auth=auth,
        public_paths=(
            frozenset()
            if static_bundle is None
            else static_bundle.public_paths
        ),
    )

    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        components.setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
        }
        schemas["ErrorBody"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["code"],
            "properties": {"code": {"type": "string"}},
        }
        schemas["ErrorResponse"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["error"],
            "properties": {
                "error": {"$ref": "#/components/schemas/ErrorBody"}
            },
        }
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        for path, path_item in schema.get("paths", {}).items():
            if (
                not isinstance(path, str)
                or not (
                    path.startswith("/api/")
                    or path == "/health"
                )
            ):
                continue
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                } or not isinstance(operation, dict):
                    continue
                operation["security"] = [{"BearerAuth": []}]
                responses = operation.setdefault("responses", {})
                for status in (
                    "400",
                    "401",
                    "403",
                    "409",
                    "422",
                    "503",
                ):
                    responses[status] = {
                        "description": "Safe error",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/"
                                        "ErrorResponse"
                                    )
                                }
                            }
                        },
                    }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    def static_response(
        request: Request,
        asset: StaticAsset,
    ) -> Response:
        return Response(
            content=(
                b"" if request.method == "HEAD" else asset.content
            ),
            media_type=asset.media_type,
        )

    if static_bundle is not None:
        index_asset = static_bundle.index

        @app.api_route(
            "/",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def web_index(request: Request) -> Response:
            return static_response(request, index_asset)

        def asset_endpoint(
            asset: StaticAsset,
        ) -> Callable[[Request], Awaitable[Response]]:
            async def endpoint(request: Request) -> Response:
                return static_response(request, asset)

            return endpoint

        for public_path, asset in static_bundle.assets.items():
            app.add_api_route(
                public_path,
                asset_endpoint(asset),
                methods=["GET", "HEAD"],
                include_in_schema=False,
            )

    @app.exception_handler(StarletteHTTPException)
    async def safe_http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        del request
        code = (
            error.detail.get("code")
            if isinstance(error.detail, dict)
            else None
        )
        return JSONResponse(
            {
                "error": {
                    "code": code if isinstance(code, str) else "request_failed"
                }
            },
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request, error
        return JSONResponse(
            {"error": {"code": "invalid_request"}},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def safe_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        del request
        if isinstance(error, ServerError):
            statuses = {
                ServerErrorCode.CONFIG_CONFLICT: 409,
                ServerErrorCode.CONFIG_NOT_FOUND: 404,
                ServerErrorCode.DIRECTORY_NOT_FOUND: 404,
                ServerErrorCode.DISCOVERY_NOT_FOUND: 404,
                ServerErrorCode.JOB_NOT_FOUND: 404,
                ServerErrorCode.INTERACTION_NOT_FOUND: 404,
                ServerErrorCode.INTERACTION_CONFLICT: 409,
                ServerErrorCode.INTERACTION_BUDGET_EXHAUSTED: 409,
                ServerErrorCode.RUN_BUSY: 409,
                ServerErrorCode.RUN_NOT_FOUND: 404,
                ServerErrorCode.RUN_DELETE_CONFLICT: 409,
                ServerErrorCode.WATCH_NOT_FOUND: 404,
                ServerErrorCode.FRESH_MAPPING_REQUIRED: 422,
                ServerErrorCode.DATABASE_UNAVAILABLE: 503,
                ServerErrorCode.PROVIDER_UNAVAILABLE: 503,
            }
            return JSONResponse(
                {"error": {"code": error.code.value}},
                status_code=statuses.get(error.code, 400),
            )
        if isinstance(error, (ApprovalError, ExecutorError)):
            return JSONResponse(
                {"error": {"code": error.code.value}},
                status_code=409,
            )
        return JSONResponse(
            {"error": {"code": "internal_error"}},
            status_code=500,
        )

    async def require_visible_run(run_id: str) -> None:
        visible = await asyncio.to_thread(
            dependencies.queries.is_run_visible,
            run_id,
        )
        if not visible:
            raise HTTPException(
                404, detail={"code": "run_not_found"}
            )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> dict[str, object]:
        if dependencies.health is None:
            raise HTTPException(
                503, detail={"code": "database_unavailable"}
            )
        try:
            result = await asyncio.to_thread(dependencies.health)
            return {
                "status": "ok",
                "postgres_major": result.postgres_major,
                "schema_version": result.schema_version,
                "notification_pending": getattr(
                    result, "notification_pending", 0
                ),
                "notification_dead": getattr(
                    result, "notification_dead", 0
                ),
                "telegram_configured": getattr(
                    result, "telegram_configured", False
                ),
            }
        except Exception:
            raise HTTPException(
                503, detail={"code": "database_unavailable"}
            ) from None

    @app.get(
        "/api/v1/session",
        response_model=SessionResponse,
    )
    async def session() -> dict[str, object]:
        return {
            "api_version": "1.0.0",
            "role": "admin",
        }

    @app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
    async def get_run(
        run_id: str,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        value = await asyncio.to_thread(
            dependencies.queries.get_run, run_id
        )
        if value is None:
            raise HTTPException(404, detail={"code": "run_not_found"})
        return value

    @app.delete(
        "/api/v1/runs/{run_id}",
        response_model=RunDeletionResponse,
    )
    async def delete_run(
        run_id: str,
        key: str = Depends(_idempotency_key),
    ) -> dict[str, object]:
        if dependencies.run_delete is None:
            raise HTTPException(503, detail={"code": "unavailable"})

        def execute() -> dict[str, object]:
            return dependencies.run_delete(run_id)

        resolve = (
            None
            if dependencies.run_delete_resolve is None
            else lambda: dependencies.run_delete_resolve(run_id)
        )
        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="run_delete",
                subject_id=run_id,
                idempotency_key=key,
                request={},
                execute=execute,
                resolve=resolve,
            )
        )

    @app.get("/api/v1/runs", response_model=RunsResponse)
    async def list_runs(
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_runs,
                    before=before,
                    limit=limit,
                )
            )
        }

    @app.get("/api/v1/discoveries", response_model=DiscoveriesResponse)
    async def list_discoveries(
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_discoveries,
                    before=before,
                    limit=limit,
                )
            )
        }

    @app.get(
        "/api/v1/folders",
        response_model=FolderObservationsResponse,
    )
    async def list_folder_observations(
        limit: int = 100,
    ) -> dict[str, object]:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_folder_observations,
                    limit=limit,
                )
            )
        }

    @app.get("/api/v1/admin/config", response_model=ConfigResponse)
    async def get_config() -> dict[str, object]:
        value = await asyncio.to_thread(
            dependencies.queries.get_config
        )
        if value is None:
            raise HTTPException(404, detail={"code": "config_not_found"})
        return value

    @app.get(
        "/api/v1/admin/directories",
        response_model=DirectoryListingResponse,
    )
    async def list_directories(path: str = "") -> dict[str, object]:
        if len(path.encode("utf-8")) > 4_096:
            raise HTTPException(
                400, detail={"code": "invalid_directory_path"}
            )
        if dependencies.directory_list is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        return await asyncio.to_thread(dependencies.directory_list, path)

    @app.put(
        "/api/v1/admin/config",
        response_model=ConfigResponse,
    )
    async def update_config(
        body: ConfigUpdateRequest,
        expected_revision: int = Depends(_config_revision),
        key: str = Depends(_idempotency_key),
    ) -> dict[str, object]:
        if dependencies.config_update is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        value = body.model_dump()
        if value["agent_budget"] is None:
            del value["agent_budget"]
        if value["telegram"] is None:
            del value["telegram"]
        if value["acgrip"] is None:
            del value["acgrip"]
        if value["subtitle_acquisition_policy"] is None:
            del value["subtitle_acquisition_policy"]

        def execute() -> dict[str, object]:
            return dependencies.config_update(expected_revision, value)

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="config_update",
                subject_id="config",
                idempotency_key=key,
                request={
                    "expected_revision": expected_revision,
                    "value": value,
                },
                execute=execute,
                resolve=(
                    None
                    if dependencies.config_resolve is None
                    else lambda: dependencies.config_resolve(
                        expected_revision, value
                    )
                ),
            )
        )

    @app.post(
        "/api/v1/admin/config/provider-probe",
        response_model=ProviderProbeResponse,
    )
    async def provider_probe(
        body: ProviderProbeRequest,
    ) -> dict[str, object]:
        del body
        if dependencies.provider_probe is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        result = await dependencies.provider_probe()
        return {
            "available": result.available,
            "status_code": result.status_code,
        }

    @app.post(
        "/api/v1/admin/config/telegram-test",
        response_model=TelegramTestResponse,
    )
    async def telegram_test(
        body: TelegramTestRequest,
        key: str = Depends(_idempotency_key),
    ) -> dict[str, object]:
        del body
        if dependencies.telegram_test is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        return await asyncio.to_thread(dependencies.telegram_test, key)

    @app.post(
        "/api/v1/admin/watches/{watch_id}/move-capability-probe",
        response_model=MoveCapabilityResponse,
    )
    async def move_capability_probe(
        watch_id: str,
        body: MoveCapabilityProbeRequest,
    ) -> dict[str, object]:
        del body
        if (
            not watch_id
            or len(watch_id.encode("utf-8")) > 128
            or dependencies.move_capability_probe is None
        ):
            raise HTTPException(404, detail={"code": "watch_not_found"})
        return await dependencies.move_capability_probe(watch_id)

    @app.get(
        "/api/v1/runs/{run_id}/plan",
        response_model=PlanLineageItem,
    )
    async def get_plan(
        run_id: str,
        version: int | None = None,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if version is not None and version < 1:
            raise HTTPException(400, detail={"code": "invalid_version"})
        value = await asyncio.to_thread(
            dependencies.queries.get_plan,
            run_id=run_id,
            version=version,
        )
        if value is None:
            raise HTTPException(404, detail={"code": "plan_not_found"})
        return value

    @app.get(
        "/api/v1/runs/{run_id}/plans",
        response_model=PlanLineageResponse,
    )
    async def list_plans(
        run_id: str,
        before_version: int | None = None,
        limit: int = 50,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if (
            before_version is not None
            and before_version < 1
        ) or not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_plans,
                    run_id=run_id,
                    before_version=before_version,
                    limit=limit,
                )
            )
        }

    @app.get(
        "/api/v1/runs/{run_id}/plans/{version}/preview",
        response_model=PlanPreviewResponse,
    )
    async def plan_preview(
        run_id: str,
        version: int,
        after: int = 0,
        limit: int = 50,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if (
            version < 1
            or after < 0
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise HTTPException(400, detail={"code": "invalid_page"})
        value = await asyncio.to_thread(
            dependencies.queries.get_plan_preview,
            run_id=run_id,
            version=version,
            after=after,
            limit=limit,
        )
        if value is None:
            raise HTTPException(404, detail={"code": "plan_not_found"})
        return value

    @app.get(
        "/api/v1/runs/{run_id}/interactions",
        response_model=InteractionHistoryResponse,
    )
    async def interaction_history(
        run_id: str,
        before: str | None = None,
        limit: int = 50,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_interactions,
                    run_id=run_id,
                    before=before,
                    limit=limit,
                )
            )
        }

    @app.get(
        "/api/v1/runs/{run_id}/events",
        response_model=EventsResponse,
    )
    async def events(
        run_id: str,
        after: int = 0,
        limit: int = 50,
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if after < 0 or not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": list(
                await asyncio.to_thread(
                    dependencies.queries.list_events,
                    run_id=run_id,
                    after_event_id=after,
                    limit=limit,
                )
            )
        }

    @app.get("/api/v1/runs/{run_id}/events/stream")
    async def event_stream(
        request: Request,
        run_id: str,
        cursor: int = Depends(_cursor),
        _: None = Depends(require_visible_run),
    ) -> StreamingResponse:
        latest = await asyncio.to_thread(
            dependencies.queries.latest_event_id, run_id
        )
        if cursor > latest:
            raise HTTPException(409, detail={"code": "cursor_ahead"})

        async def stream() -> AsyncIterator[str]:
            current = cursor
            empty_polls = 0
            last_emit = time.monotonic()
            while (
                dependencies.sse_max_empty_polls is None
                or empty_polls < dependencies.sse_max_empty_polls
            ):
                if await request.is_disconnected():
                    return
                rows = await asyncio.to_thread(
                    dependencies.queries.list_events,
                    run_id=run_id,
                    after_event_id=current,
                    limit=50,
                )
                if not rows:
                    empty_polls += 1
                    if (
                        time.monotonic() - last_emit
                        >= dependencies.sse_heartbeat_seconds
                    ):
                        yield ": keepalive\n\n"
                        last_emit = time.monotonic()
                    await asyncio.sleep(dependencies.sse_poll_seconds)
                    continue
                empty_polls = 0
                for item in rows:
                    event_id = int(item["event_id"])
                    data = json.dumps(
                        {
                            "event_type": item["event_type"],
                            "data": item["data"],
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    yield (
                        f"id: {event_id}\n"
                        "event: run_event\n"
                        f"data: {data}\n\n"
                    )
                    current = event_id
                    last_emit = time.monotonic()
            yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no"},
        )

    @app.post(
        "/api/v1/runs/{run_id}/interactions",
        response_model=InteractionResponse,
    )
    async def interact(
        run_id: str,
        body: InteractionRequest,
        key: str = Depends(_idempotency_key),
        plan_hash: str = Depends(_plan_hash),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.interactions is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        kind = InteractionKind(body.kind)
        message = _text(body.message)
        result = await _shield_thread(
            lambda: dependencies.interactions.run(
                run_id=run_id,
                kind=kind,
                idempotency_key=key,
                expected_plan_hash=plan_hash,
                message=message,
            )
        )
        if (
            kind is InteractionKind.REVISION
            and result.plan_hash is not None
            and dependencies.folder_dispositions is not None
        ):
            await _shield_thread(
                lambda: dependencies.folder_dispositions.prepare_success(
                    run_id=run_id,
                    media_plan_hash=result.plan_hash,
                )
            )
        return {
            "interaction_id": result.interaction_id,
            "kind": result.kind.value,
            "assistant_reply": result.assistant_reply,
            "plan_hash": result.plan_hash,
            "model_tokens": result.model_tokens,
        }

    @app.post(
        "/api/v1/runs/{run_id}/reapply",
        response_model=ReapplyResponse,
    )
    async def reapply(
        run_id: str,
        body: ReapplyRequest,
        key: str = Depends(_idempotency_key),
        plan_hash: str = Depends(_plan_hash),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.interactions is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        message = _text(body.message)
        result = await _shield_thread(
            lambda: dependencies.interactions.run(
                run_id=run_id,
                kind=InteractionKind.REAPPLY,
                idempotency_key=key,
                expected_plan_hash=plan_hash,
                message=message,
            )
        )
        return {
            "interaction_id": result.interaction_id,
            "assistant_reply": result.assistant_reply,
            "plan_hash": result.plan_hash,
            "no_op": result.plan_hash is None,
        }

    @app.post(
        "/api/v1/runs/{run_id}/approve-and-apply",
        response_model=ApplyResponse,
    )
    async def approve_and_apply(
        run_id: str,
        body: ApproveApplyRequest,
        key: str = Depends(_idempotency_key),
        plan_hash: str = Depends(_plan_hash),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.apply is None:
            raise HTTPException(503, detail={"code": "unavailable"})

        def payload(
            result: ApplyResult,
            folder_result: FolderDispositionResult | None = None,
        ) -> dict[str, object]:
            return {
                "transaction_id": result.transaction_id,
                "plan_hash": result.plan_hash,
                "approval_id": result.approval_id,
                "status": result.status.value,
                "applied_count": result.applied_count,
                "rolled_back_count": result.rolled_back_count,
                "folder_disposition": (
                    None
                    if folder_result is None
                    else _folder_disposition_payload(folder_result)
                ),
            }

        def execute() -> dict[str, object]:
            prepared = (
                None
                if dependencies.folder_dispositions is None
                else dependencies.folder_dispositions.prepare_success(
                    run_id=run_id,
                    media_plan_hash=plan_hash,
                )
            )
            if (
                prepared is None
                and body.folder_disposition_plan_hash is not None
            ) or (
                prepared is not None
                and body.folder_disposition_plan_hash
                != prepared.plan_hash
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            result = dependencies.apply.approve_and_apply(
                run_id=run_id,
                plan_hash=plan_hash,
                automatic=body.automatic,
            )
            if (
                result.status.value == "rolled_back"
                and result.failure_code
                is ExecutorErrorCode.DESTINATION_COLLISION
                and dependencies.folder_dispositions is not None
            ):
                dependencies.folder_dispositions.prepare_failure(
                    run_id=run_id,
                    reason_code="executor_destination_collision",
                )
            folder_result = None
            if (
                result.status.value == "completed"
                and prepared is not None
                and dependencies.folder_dispositions is not None
            ):
                try:
                    folder_result = (
                        dependencies.folder_dispositions
                        .approve_and_execute(
                            run_id=run_id,
                            plan_hash=prepared.plan_hash,
                            automatic=body.automatic,
                        )
                    )
                except (ExecutorError, ApprovalError):
                    folder_result = None
            return payload(result, folder_result)

        def resolve() -> dict[str, object] | None:
            result = dependencies.apply.resolve(
                run_id=run_id,
                plan_hash=plan_hash,
            )
            if result is None:
                return None
            folder_result = None
            folder_hash = body.folder_disposition_plan_hash
            if (
                folder_hash is not None
                and dependencies.folder_dispositions is not None
            ):
                folder_result = dependencies.folder_dispositions.resolve(
                    run_id=run_id,
                    plan_hash=folder_hash,
                )
            return payload(result, folder_result)

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="approve_apply",
                subject_id=run_id,
                idempotency_key=key,
                request={
                    "automatic": body.automatic,
                    "folder_disposition_plan_hash": (
                        body.folder_disposition_plan_hash
                    ),
                    "plan_hash": plan_hash,
                },
                execute=execute,
                resolve=resolve,
            )
        )

    @app.post(
        "/api/v1/runs/{run_id}/subtitle-acquisition/approve",
        response_model=SubtitleAcquisitionResponse,
    )
    async def approve_subtitle_acquisition(
        run_id: str,
        body: SubtitleAcquisitionApprovalRequest,
        key: str = Depends(_idempotency_key),
        plan_hash: str = Depends(_plan_hash),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        del body
        coordinator = dependencies.subtitle_acquisitions
        if coordinator is None:
            raise HTTPException(503, detail={"code": "unavailable"})

        def payload(
            record: SubtitleAcquisitionRequestRecord,
        ) -> dict[str, object]:
            return {
                "run_id": record.run_id,
                "plan_hash": record.plan_hash,
                "policy": record.policy.value,
                "status": record.status,
                "approval_id": record.approval_id,
                "transaction_id": record.transaction_id,
                "failure_code": record.failure_code,
                "successor_status": None,
            }

        def execute() -> dict[str, object]:
            return payload(
                coordinator.approve_and_execute(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    automatic=False,
                )
            )

        def resolve() -> dict[str, object] | None:
            record = coordinator.resolve(
                run_id=run_id,
                plan_hash=plan_hash,
            )
            return None if record is None else payload(record)

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="approve_subtitle_acquisition",
                subject_id=run_id,
                idempotency_key=key,
                request={"plan_hash": plan_hash},
                execute=execute,
                resolve=resolve,
            )
        )

    @app.post(
        "/api/v1/runs/{run_id}/folder-disposition",
        response_model=FolderDispositionResultResponse,
    )
    async def execute_folder_disposition(
        run_id: str,
        body: FolderDispositionRequest,
        key: str = Depends(_idempotency_key),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.folder_dispositions is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        if body.automatic:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)

        def execute() -> dict[str, object]:
            return _folder_disposition_payload(
                dependencies.folder_dispositions.approve_and_execute(
                    run_id=run_id,
                    plan_hash=body.plan_hash,
                    automatic=False,
                )
            )

        def resolve() -> dict[str, object] | None:
            result = dependencies.folder_dispositions.resolve(
                run_id=run_id,
                plan_hash=body.plan_hash,
            )
            return (
                None
                if result is None
                else _folder_disposition_payload(result)
            )

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="folder_disposition",
                subject_id=run_id,
                idempotency_key=key,
                request={
                    "automatic": False,
                    "plan_hash": body.plan_hash,
                },
                execute=execute,
                resolve=resolve,
            )
        )

    @app.post(
        "/api/v1/operations/runs/{run_id}/folder-disposition/recover",
        response_model=FolderDispositionResultResponse,
    )
    async def recover_folder_disposition(
        run_id: str,
        body: FolderDispositionRecoveryRequest,
        key: str = Depends(_idempotency_key),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.folder_dispositions is None:
            raise HTTPException(503, detail={"code": "unavailable"})

        def execute() -> dict[str, object]:
            return _folder_disposition_payload(
                dependencies.folder_dispositions.recover(
                    run_id=run_id,
                    plan_hash=body.plan_hash,
                    approval_id=body.approval_id,
                )
            )

        def resolve() -> dict[str, object] | None:
            result = dependencies.folder_dispositions.resolve(
                run_id=run_id,
                plan_hash=body.plan_hash,
                approval_id=body.approval_id,
            )
            return (
                None
                if result is None
                else _folder_disposition_payload(result)
            )

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="folder_disposition_recover",
                subject_id=run_id,
                idempotency_key=key,
                request={
                    "approval_id": body.approval_id,
                    "plan_hash": body.plan_hash,
                },
                execute=execute,
                resolve=resolve,
            )
        )

    @app.post(
        "/api/v1/operations/runs/{run_id}/recover",
        response_model=RecoveryResponse,
    )
    async def recover(
        run_id: str,
        body: RecoveryRequest,
        key: str = Depends(_idempotency_key),
        plan_hash: str = Depends(_plan_hash),
        _: None = Depends(require_visible_run),
    ) -> dict[str, object]:
        if dependencies.apply is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        approval_id = _text(body.approval_id)

        def payload(result: ApplyResult) -> dict[str, object]:
            return {
                "transaction_id": result.transaction_id,
                "status": result.status.value,
                "applied_count": result.applied_count,
                "rolled_back_count": result.rolled_back_count,
            }

        def execute() -> dict[str, object]:
            return payload(
                dependencies.apply.recover(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    approval_id=approval_id,
                )
            )

        def resolve() -> dict[str, object] | None:
            result = dependencies.apply.resolve(
                run_id=run_id,
                plan_hash=plan_hash,
                approval_id=approval_id,
            )
            return None if result is None else payload(result)

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="recover",
                subject_id=run_id,
                idempotency_key=key,
                request={
                    "approval_id": approval_id,
                    "plan_hash": plan_hash,
                },
                execute=execute,
                resolve=resolve,
            )
        )

    return app
