from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reeloom.executor.apply import ApplyResult
from reeloom.server.auth import AuthSettings, Role
from reeloom.server.interactions import (
    InteractionKind,
    InteractionService,
)
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.executor.errors import (
    ApprovalError,
    ExecutorError,
)

_MAX_BODY_BYTES = 64 * 1024
_MAX_PAGE_SIZE = 100
_BODY_TIMEOUT_SECONDS = 5.0
_EMPTY_SSE_POLLS = 2
_SSE_POLL_SECONDS = 0.25
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


class ApiQueries(Protocol):
    def get_run(self, run_id: str) -> dict[str, object] | None: ...

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


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    queries: ApiQueries
    interactions: InteractionService | None = None
    apply: ApplyCoordinator | None = None
    health: Callable[[], object] | None = None
    config_update: (
        Callable[[int, dict[str, object]], dict[str, object]] | None
    ) = None
    config_resolve: (
        Callable[[int, dict[str, object]], dict[str, object] | None]
        | None
    ) = None
    provider_probe: Callable[[], Awaitable[object]] | None = None
    idempotency: object | None = None


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
    def __init__(self, app: ASGIApp, *, auth: AuthSettings) -> None:
        self.app = app
        self._auth = auth
        self._concurrency = threading.BoundedSemaphore(64)
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
        if not self._concurrency.acquire(blocking=False):
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
            if scope["path"] == "/health":
                async def health_send(message: Message) -> None:
                    if message["type"] == "http.response.start":
                        message["headers"] = list(
                            message.get("headers", [])
                        ) + self._headers(origin)
                    await send(message)

                await self.app(scope, receive, health_send)
                return
            authorization = headers.get("authorization", "")
            prefix = "Bearer "
            role = (
                self._auth.authenticate(authorization[len(prefix) :])
                if authorization.startswith(prefix)
                else None
            )
            if role is None:
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
            scope.setdefault("state", {})["role"] = role
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
            self._concurrency.release()

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
                        b"GET, POST, PUT, OPTIONS",
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


def _require(minimum: Role) -> Callable[[Request], object]:
    async def dependency(request: Request) -> Role:
        role = request.state.role
        if role < minimum:
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden"},
            )
        return role

    return dependency


def _cursor(request: Request) -> int:
    raw = request.headers.get("last-event-id", "0")
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


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not value or len(value.encode("utf-8")) > 256:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return value


def _plan_hash(request: Request) -> str:
    value = request.headers.get("if-match", "")
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


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(400, detail={"code": "invalid_body"})
    return value


async def _json_object(
    request: Request,
    *,
    fields: frozenset[str],
) -> dict[str, object]:
    try:
        value = await request.json()
    except Exception:
        raise HTTPException(400, detail={"code": "invalid_json"}) from None
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise HTTPException(400, detail={"code": "invalid_body"})
    return value


async def _shield_thread(function: Callable[[], object]) -> object:
    task = asyncio.create_task(asyncio.to_thread(function))
    return await asyncio.shield(task)


def create_api(
    dependencies: ApiDependencies,
    *,
    auth: AuthSettings,
) -> FastAPI:
    app = FastAPI(
        title="Reeloom API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.add_middleware(_SecurityBoundary, auth=auth)

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
                ServerErrorCode.DISCOVERY_NOT_FOUND: 404,
                ServerErrorCode.JOB_NOT_FOUND: 404,
                ServerErrorCode.INTERACTION_NOT_FOUND: 404,
                ServerErrorCode.INTERACTION_CONFLICT: 409,
                ServerErrorCode.RUN_BUSY: 409,
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

    @app.get("/health")
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
            }
        except Exception:
            raise HTTPException(
                503, detail={"code": "database_unavailable"}
            ) from None

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(
        run_id: str,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> dict[str, object]:
        del role
        value = await asyncio.to_thread(
            dependencies.queries.get_run, run_id
        )
        if value is None:
            raise HTTPException(404, detail={"code": "run_not_found"})
        return value

    @app.get("/api/v1/runs")
    async def list_runs(
        before: str | None = None,
        limit: int = 50,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> dict[str, object]:
        del role
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": await asyncio.to_thread(
                dependencies.queries.list_runs,
                before=before,
                limit=limit,
            )
        }

    @app.get("/api/v1/discoveries")
    async def list_discoveries(
        before: str | None = None,
        limit: int = 50,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> dict[str, object]:
        del role
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": await asyncio.to_thread(
                dependencies.queries.list_discoveries,
                before=before,
                limit=limit,
            )
        }

    @app.get("/api/v1/admin/config")
    async def get_config(
        role: Role = Depends(_require(Role.ADMIN)),
    ) -> dict[str, object]:
        del role
        value = await asyncio.to_thread(
            dependencies.queries.get_config
        )
        if value is None:
            raise HTTPException(404, detail={"code": "config_not_found"})
        return value

    @app.put("/api/v1/admin/config")
    async def update_config(
        request: Request,
        role: Role = Depends(_require(Role.ADMIN)),
    ) -> dict[str, object]:
        del role
        if dependencies.config_update is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        raw_revision = request.headers.get("if-match", "")
        try:
            expected_revision = int(raw_revision)
        except ValueError:
            raise HTTPException(
                400, detail={"code": "invalid_revision"}
            ) from None
        value = await _json_object(
            request,
            fields=frozenset(
                {
                    "apply_policy",
                    "archive_routes",
                    "provider",
                    "watches",
                }
            ),
        )
        key = _idempotency_key(request)

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

    @app.post("/api/v1/admin/config/provider-probe")
    async def provider_probe(
        request: Request,
        role: Role = Depends(_require(Role.ADMIN)),
    ) -> dict[str, object]:
        del role
        await _json_object(request, fields=frozenset())
        if dependencies.provider_probe is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        result = await dependencies.provider_probe()
        return {
            "available": result.available,
            "status_code": result.status_code,
        }

    @app.get("/api/v1/runs/{run_id}/plan")
    async def get_plan(
        run_id: str,
        version: int | None = None,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> dict[str, object]:
        del role
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

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        after: int = 0,
        limit: int = 50,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> dict[str, object]:
        del role
        if after < 0 or not 1 <= limit <= _MAX_PAGE_SIZE:
            raise HTTPException(400, detail={"code": "invalid_page"})
        return {
            "items": await asyncio.to_thread(
                dependencies.queries.list_events,
                run_id=run_id,
                after_event_id=after,
                limit=limit,
            )
        }

    @app.get("/api/v1/runs/{run_id}/events/stream")
    async def event_stream(
        request: Request,
        run_id: str,
        role: Role = Depends(_require(Role.VIEWER)),
    ) -> StreamingResponse:
        del role
        cursor = _cursor(request)
        latest = await asyncio.to_thread(
            dependencies.queries.latest_event_id, run_id
        )
        if cursor > latest:
            raise HTTPException(409, detail={"code": "cursor_ahead"})

        async def stream() -> AsyncIterator[str]:
            current = cursor
            empty_polls = 0
            while empty_polls < _EMPTY_SSE_POLLS:
                rows = await asyncio.to_thread(
                    dependencies.queries.list_events,
                    run_id=run_id,
                    after_event_id=current,
                    limit=50,
                )
                if not rows:
                    empty_polls += 1
                    await asyncio.sleep(_SSE_POLL_SECONDS)
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
            yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no"},
        )

    @app.post("/api/v1/runs/{run_id}/interactions")
    async def interact(
        request: Request,
        run_id: str,
        role: Role = Depends(_require(Role.OPERATOR)),
    ) -> dict[str, object]:
        del role
        if dependencies.interactions is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        value = await _json_object(
            request,
            fields=frozenset({"kind", "message"}),
        )
        try:
            kind = InteractionKind(_text(value["kind"]))
        except ValueError:
            raise HTTPException(
                400, detail={"code": "invalid_interaction_kind"}
            ) from None
        if kind is InteractionKind.REAPPLY:
            raise HTTPException(400, detail={"code": "use_reapply_route"})
        key = _idempotency_key(request)
        plan_hash = _plan_hash(request)
        message = _text(value["message"])
        result = await _shield_thread(
            lambda: dependencies.interactions.run(
                run_id=run_id,
                kind=kind,
                idempotency_key=key,
                expected_plan_hash=plan_hash,
                message=message,
            )
        )
        return {
            "interaction_id": result.interaction_id,
            "kind": result.kind.value,
            "assistant_reply": result.assistant_reply,
            "plan_hash": result.plan_hash,
            "model_tokens": result.model_tokens,
        }

    @app.post("/api/v1/runs/{run_id}/reapply")
    async def reapply(
        request: Request,
        run_id: str,
        role: Role = Depends(_require(Role.OPERATOR)),
    ) -> dict[str, object]:
        del role
        if dependencies.interactions is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        value = await _json_object(
            request,
            fields=frozenset({"message"}),
        )
        key = _idempotency_key(request)
        plan_hash = _plan_hash(request)
        message = _text(value["message"])
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

    @app.post("/api/v1/runs/{run_id}/approve-and-apply")
    async def approve_and_apply(
        request: Request,
        run_id: str,
        role: Role = Depends(_require(Role.OPERATOR)),
    ) -> dict[str, object]:
        del role
        if dependencies.apply is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        value = await _json_object(
            request,
            fields=frozenset({"automatic"}),
        )
        if type(value["automatic"]) is not bool:
            raise HTTPException(400, detail={"code": "invalid_body"})
        key = _idempotency_key(request)
        plan_hash = _plan_hash(request)

        def payload(result: ApplyResult) -> dict[str, object]:
            return {
                "transaction_id": result.transaction_id,
                "plan_hash": result.plan_hash,
                "approval_id": result.approval_id,
                "status": result.status.value,
                "applied_count": result.applied_count,
                "rolled_back_count": result.rolled_back_count,
            }

        def execute() -> dict[str, object]:
            return payload(
                dependencies.apply.approve_and_apply(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    automatic=value["automatic"],
                )
            )

        def resolve() -> dict[str, object] | None:
            result = dependencies.apply.resolve(
                run_id=run_id,
                plan_hash=plan_hash,
            )
            return None if result is None else payload(result)

        if dependencies.idempotency is None:
            return await _shield_thread(execute)
        return await _shield_thread(
            lambda: dependencies.idempotency.run(
                scope="approve_apply",
                subject_id=run_id,
                idempotency_key=key,
                request={
                    "automatic": value["automatic"],
                    "plan_hash": plan_hash,
                },
                execute=execute,
                resolve=resolve,
            )
        )

    @app.post("/api/v1/operations/runs/{run_id}/recover")
    async def recover(
        request: Request,
        run_id: str,
        role: Role = Depends(_require(Role.OPERATOR)),
    ) -> dict[str, object]:
        del role
        if dependencies.apply is None:
            raise HTTPException(503, detail={"code": "unavailable"})
        value = await _json_object(
            request,
            fields=frozenset({"approval_id"}),
        )
        key = _idempotency_key(request)
        plan_hash = _plan_hash(request)
        approval_id = _text(value["approval_id"])

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
