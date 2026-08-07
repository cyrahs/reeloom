from __future__ import annotations

import json
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.server.api import (
    ApiDependencies,
    _SecurityBoundary,
    _safe_executor_error_context,
    create_api,
)
from reeloom.server.auth import AuthSettings
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.interactions import InteractionKind, InteractionResult
from reeloom.server.queries import _safe_event
from reeloom.server.config import SubtitleAcquisitionPolicy
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionRequestRecord,
)


def test_executor_error_context_is_bounded_and_allowlisted() -> None:
    context = _safe_executor_error_context(
        ExecutorError(
            ExecutorErrorCode.RECOVERY_REQUIRED,
            context={
                "candidate_id": "video:1",
                "source_relative_path": "episode.mkv",
                "source_state": "absent",
                "destination_relative_path": "Series/episode.mkv",
                "destination_state": "absent",
                "secret": "not-returned",
                "oversized": "x" * 5_000,
            },
        )
    )

    assert context == {
        "candidate_id": "video:1",
        "destination_relative_path": "Series/episode.mkv",
        "destination_state": "absent",
        "source_relative_path": "episode.mkv",
        "source_state": "absent",
    }


@dataclass
class _Queries:
    def is_run_visible(self, run_id: str) -> bool:
        return run_id == "run-1"

    def list_runs(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        del before, limit
        return ()

    def list_discoveries(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        del before, limit
        return ()

    def get_run(self, run_id: str) -> dict[str, object] | None:
        if run_id != "run-1":
            return None
        return {
            "run_id": "run-1",
            "status": "awaiting_approval",
            "work_type": "anime",
            "phase": "awaiting_approval",
            "runtime_status": "paused",
            "event_sequence": 2,
            "model_turns": 1,
            "model_tokens": 20,
            "tool_calls": 1,
            "failures": 0,
            "plan_hash": "sha256:" + "a" * 64,
            "recovery_approval_id": None,
            "apply_policy": "manual",
            "available_actions": [
                "question",
                "revision",
                "approve_apply",
            ],
            "settlement": None,
        }

    def list_events(
        self,
        *,
        run_id: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        del limit
        if run_id != "run-1" or after_event_id >= 2:
            return ()
        return (
            {
                "event_id": 2,
                "event_type": "PlanBuilt",
                "data": {"plan_hash": "sha256:" + "a" * 64},
            },
        )

    def latest_event_id(self, run_id: str) -> int:
        return 2 if run_id == "run-1" else 0

    def get_config(self) -> dict[str, object] | None:
        return {
            "revision": 1,
            "revision_id": "revision-1",
            "watches": [
                {
                    "watch_id": "w",
                    "work_type": "anime",
                    "poll_interval_seconds": 30,
                    "settle_interval_seconds": 120,
                    "root": "/media/incoming",
                    "library_root": "/media/library",
                }
            ],
            "provider": {
                "base_url": "https://provider.invalid/v1",
                "model": "gpt-5",
                "reasoning_effort": "medium",
                "verbosity": "medium",
                "api_key_configured": True,
            },
            "telegram": {
                "enabled": False,
                "notification_types": ["attention_required"],
                "destination_configured": False,
            },
            "apply_policy": "manual",
            "acgrip": {"enabled": False},
            "subtitle_acquisition_policy": "automatic",
            "agent_budget": {
                "max_model_turns": 64,
                "max_tool_calls": 64,
                "max_failures": 3,
                "max_total_tokens": 100_000,
                "max_elapsed_seconds": 600,
            },
        }

    def get_plan(
        self,
        *,
        run_id: str,
        version: int | None,
    ) -> dict[str, object] | None:
        del version
        return (
            {
                "run_id": run_id,
                "version": 1,
                "plan_hash": "sha256:" + "a" * 64,
                "parent_plan_hash": None,
                "plan_kind": "initial",
                "created_at": "2026-07-26T00:00:00+00:00",
            }
            if run_id == "run-1"
            else None
        )

    def list_plans(
        self,
        *,
        run_id: str,
        before_version: int | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        del before_version, limit
        if run_id != "run-1":
            return ()
        return (
            {
                "run_id": run_id,
                "version": 1,
                "plan_hash": "sha256:" + "a" * 64,
                "parent_plan_hash": None,
                "plan_kind": "initial",
                "created_at": "2026-07-26T00:00:00+00:00",
            },
        )

    def get_plan_preview(
        self,
        *,
        run_id: str,
        version: int,
        after: int,
        limit: int,
    ) -> dict[str, object] | None:
        del after, limit
        if run_id != "run-1" or version != 1:
            return None
        return {
            "run_id": run_id,
            "version": 1,
            "plan_hash": "sha256:" + "a" * 64,
            "plan_kind": "initial",
            "counts": {"move": 1, "unmapped": 0, "unchanged": 0},
            "review": {
                "status": "system_only",
                "agent_summary": None,
                "advisory_only": True,
                "coverage": {
                    "total_unmapped": 0,
                    "agent_explained": 0,
                    "system_verified": 0,
                    "fallback": 0,
                },
            },
            "items": [
                {
                    "index": 0,
                    "disposition": "move",
                    "candidate_id": "video:1",
                    "kind": "video",
                    "source": "<script>alert(1)</script>.mkv",
                    "destination": "Series (2026)/Season 01/Series - S01E01.mkv",
                    "explanation": None,
                }
            ],
            "next_after": None,
        }

    def list_interactions(
        self,
        *,
        run_id: str,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        del before, limit
        if run_id != "run-1":
            return ()
        return (
            {
                "interaction_id": "interaction-1",
                "kind": "question",
                "status": "completed",
                "request_message": "<img src=x onerror=alert(1)>",
                "assistant_reply": "Plain reply",
                "content_available": True,
                "plan_hash": None,
                "created_at": "2026-07-26T00:00:00+00:00",
                "finished_at": "2026-07-26T00:00:01+00:00",
            },
        )


def _app(
    *,
    static_root: Path | None = None,
    directory_list: Callable[[str], dict[str, object]] | None = None,
    run_delete: Callable[[str], dict[str, object]] | None = None,
    move_capability_probe: Callable[..., object] | None = None,
    telegram_test: Callable[[str], dict[str, object]] | None = None,
    subtitle_acquisitions: object | None = None,
    interactions: object | None = None,
    attention_retry: Callable[[str, int], dict[str, object]] | None = None,
    attention_fail: Callable[[str, int], dict[str, object]] | None = None,
) -> object:
    app = create_api(
        ApiDependencies(
            queries=_Queries(),
            directory_list=directory_list,
            run_delete=run_delete,
            move_capability_probe=move_capability_probe,  # type: ignore[arg-type]
            telegram_test=telegram_test,
            subtitle_acquisitions=subtitle_acquisitions,  # type: ignore[arg-type]
            interactions=interactions,  # type: ignore[arg-type]
            attention_retry=attention_retry,
            attention_fail=attention_fail,
        ),
        auth=AuthSettings.create(
            admin_token="admin-token-strong",
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        ),
        static_root=static_root,
    )
    return app


class _Interactions:
    def __init__(self) -> None:
        self.head: tuple[str | None, int | None] | None = None

    def run(self, **values: object) -> InteractionResult:
        self.head = (
            values["expected_plan_hash"],  # type: ignore[index]
            values["expected_event_sequence"],  # type: ignore[index]
        )
        return InteractionResult(
            interaction_id="interaction-1",
            kind=InteractionKind.QUESTION,
            assistant_reply="The evidence was ambiguous.",
            plan_hash=None,
            model_tokens=5,
        )


class _SubtitleAcquisitions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.retry_calls: list[tuple[str, str]] = []
        self.fail_calls: list[tuple[str, str]] = []

    def approve_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool,
    ) -> SubtitleAcquisitionRequestRecord:
        self.calls.append((run_id, plan_hash, automatic))
        return SubtitleAcquisitionRequestRecord(
            run_id=run_id,
            plan_hash=plan_hash,
            policy=SubtitleAcquisitionPolicy.MANUAL,
            status="published",
            approval_id="approval-subtitle-1",
            transaction_id="subtitle-txn-v1-" + "c" * 64,
        )

    def retry_blocked_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord:
        self.retry_calls.append((run_id, plan_hash))
        return SubtitleAcquisitionRequestRecord(
            run_id=run_id,
            plan_hash=plan_hash,
            policy=SubtitleAcquisitionPolicy.AUTOMATIC,
            status="published",
            approval_id="approval-subtitle-1",
            transaction_id="subtitle-txn-v1-" + "c" * 64,
        )

    def resolve(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord | None:
        del run_id, plan_hash
        return None

    def fail_blocked(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord:
        self.fail_calls.append((run_id, plan_hash))
        return SubtitleAcquisitionRequestRecord(
            run_id=run_id,
            plan_hash=plan_hash,
            policy=SubtitleAcquisitionPolicy.AUTOMATIC,
            status="blocked",
            approval_id="approval-subtitle-1",
            failure_code="source_drift",
        )

    def resolve_failed(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord | None:
        del run_id, plan_hash
        return None


def test_admin_can_approve_independent_subtitle_acquisition() -> None:
    acquisitions = _SubtitleAcquisitions()
    plan_hash = "sha256:" + "b" * 64

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(subtitle_acquisitions=acquisitions)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/runs/run-1/subtitle-acquisition/approve",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": "subtitle-approve-1",
                    "if-match": plan_hash,
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "plan_hash": plan_hash,
        "policy": "manual",
        "status": "published",
        "approval_id": "approval-subtitle-1",
        "transaction_id": "subtitle-txn-v1-" + "c" * 64,
        "failure_code": None,
        "failure_diagnostic": None,
        "successor_status": None,
    }
    assert acquisitions.calls == [("run-1", plan_hash, False)]


def test_admin_can_retry_blocked_subtitle_acquisition() -> None:
    acquisitions = _SubtitleAcquisitions()
    plan_hash = "sha256:" + "b" * 64

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(subtitle_acquisitions=acquisitions)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/runs/run-1/subtitle-acquisition/retry",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": "subtitle-retry-1",
                    "if-match": plan_hash,
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "plan_hash": plan_hash,
        "policy": "automatic",
        "status": "published",
        "approval_id": "approval-subtitle-1",
        "transaction_id": "subtitle-txn-v1-" + "c" * 64,
        "failure_code": None,
        "failure_diagnostic": None,
        "successor_status": None,
    }
    assert acquisitions.retry_calls == [("run-1", plan_hash)]


def test_admin_can_end_blocked_subtitle_acquisition() -> None:
    acquisitions = _SubtitleAcquisitions()
    plan_hash = "sha256:" + "b" * 64

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(subtitle_acquisitions=acquisitions)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/runs/run-1/subtitle-acquisition/fail",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": "subtitle-fail-1",
                    "if-match": plan_hash,
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "plan_hash": plan_hash,
        "status": "failed",
        "failure_code": "source_drift",
    }
    assert acquisitions.fail_calls == [("run-1", plan_hash)]


def test_admin_can_enqueue_idempotent_telegram_test() -> None:
    keys: list[str] = []

    def enqueue(key: str) -> dict[str, object]:
        keys.append(key)
        return {"notification_id": "notification-1", "state": "queued"}

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(telegram_test=enqueue)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/admin/config/telegram-test",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": "telegram-test-1",
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "notification_id": "notification-1",
        "state": "queued",
    }
    assert keys == ["telegram-test-1"]


def test_admin_can_probe_exact_watch_move_capability() -> None:
    async def probe(watch_id: str) -> dict[str, object]:
        assert watch_id == "watch-1"
        return {
            "watch_id": watch_id,
            "move_backend": "native",
            "folder_disposition": {
                "status": "supported",
                "failure_code": None,
            },
            "media_apply": {
                "status": "unsupported",
                "failure_code": "atomic_move_unsupported",
            },
        }

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(move_capability_probe=probe)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/admin/watches/watch-1/move-capability-probe",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json()["media_apply"] == {
        "status": "unsupported",
        "failure_code": "atomic_move_unsupported",
    }


def test_move_probe_exposes_fuse_degraded_backend() -> None:
    async def probe(watch_id: str) -> dict[str, object]:
        return {
            "watch_id": watch_id,
            "move_backend": "fuse_checked_rename",
            "folder_disposition": {
                "status": "degraded",
                "failure_code": None,
            },
            "media_apply": {
                "status": "degraded",
                "failure_code": None,
            },
        }

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(move_capability_probe=probe)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return await client.post(
                "/api/v1/admin/watches/watch-1/move-capability-probe",
                json={},
                headers={
                    "authorization": "Bearer admin-token-strong",
                },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json()["move_backend"] == "fuse_checked_rename"


def test_admin_can_delete_an_eligible_run_record() -> None:
    deleted: list[str] = []

    def delete(run_id: str) -> dict[str, object]:
        deleted.append(run_id)
        return {
            "run_id": run_id,
            "deleted_at": "2026-07-28T10:00:00+00:00",
        }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(run_delete=delete)),
            base_url="http://reeloom.test",
        ) as client:
            missing_key = await client.delete(
                "/api/v1/runs/run-1",
                headers={"authorization": "Bearer admin-token-strong"},
            )
            result = await client.delete(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": "delete-run-1",
                },
            )
            return missing_key, result

    missing_key, result = asyncio.run(scenario())

    assert missing_key.status_code == 400
    assert result.status_code == 200
    assert result.json()["run_id"] == "run-1"
    assert deleted == ["run-1"]


def test_admin_can_list_bounded_pod_directories() -> None:
    def directory_list(path: str) -> dict[str, object]:
        assert path == "mnt"
        return {
            "path": "mnt",
            "absolute_path": "/mnt",
            "parent": "",
            "directories": [{"name": "media", "path": "mnt/media"}],
        }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(directory_list=directory_list)
            ),
            base_url="http://reeloom.test",
        ) as client:
            unauthorized = await client.get(
                "/api/v1/admin/directories?path=mnt"
            )
            authorized = await client.get(
                "/api/v1/admin/directories?path=mnt",
                headers={
                    "authorization": "Bearer admin-token-strong",
                },
            )
            return unauthorized, authorized

    unauthorized, authorized = asyncio.run(scenario())

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["absolute_path"] == "/mnt"


def test_sse_connections_do_not_consume_regular_request_slots() -> None:
    async def downstream(
        scope: object,
        receive: object,
        send: object,
    ) -> None:
        del scope, receive, send

    boundary = _SecurityBoundary(
        downstream,
        auth=AuthSettings.create(
            admin_token="admin-token-strong",
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        ),
    )

    assert boundary._concurrency_gate(
        "/api/v1/runs/run-1/events/stream"
    ) is not boundary._concurrency_gate("/api/v1/runs/run-1")


def test_static_ui_is_public_only_for_manifest_paths(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text(
        '<script src="/assets/app-deadbeef.js"></script>',
        encoding="utf-8",
    )
    (assets / "app-deadbeef.js").write_text(
        "document.body.dataset.ready = 'yes';",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/app-deadbeef.js",
                    "isEntry": True,
                }
            }
        ),
        encoding="utf-8",
    )

    async def scenario() -> tuple[httpx.Response, ...]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(static_root=tmp_path)
            ),
            base_url="http://reeloom.test",
        ) as client:
            return (
                await client.get("/"),
                await client.head("/"),
                await client.get("/assets/app-deadbeef.js"),
                await client.get("/assets/not-in-manifest.js"),
                await client.get("/.env"),
                await client.get("/nested/route"),
            )

    index, head, asset, unknown, dotfile, fallback = asyncio.run(
        scenario()
    )
    assert index.status_code == 200
    assert head.status_code == 200
    assert head.content == b""
    assert asset.status_code == 200
    assert unknown.status_code == 401
    assert dotfile.status_code == 401
    assert fallback.status_code == 401
    for response in (index, head, asset):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers[
            "content-security-policy"
        ]
        assert response.headers["permissions-policy"].startswith("camera=()")


def test_openapi_declares_bearer_security_and_safe_errors() -> None:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            return await client.get(
                "/api/v1/openapi.json",
                headers={
                    "authorization": "Bearer admin-token-strong"
                },
            )

    response = asyncio.run(scenario())
    schema = response.json()
    assert response.status_code == 200
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque",
    }
    for path, path_item in schema["paths"].items():
        if not (path.startswith("/api/") or path == "/health"):
            continue
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                assert operation["security"] == [{"BearerAuth": []}]
    operation = schema["paths"]["/api/v1/session"]["get"]
    assert operation["responses"]["401"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}


def test_openapi_uses_named_strict_ui_contracts() -> None:
    schema = _app().openapi()
    paths = schema["paths"]
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete"}:
                continue
            if path.endswith("/events/stream"):
                continue
            response_schema = operation["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
            assert "$ref" in response_schema, (method, path)
            if method in {"post", "put"}:
                request_schema = operation["requestBody"]["content"][
                    "application/json"
                ]["schema"]
                assert "$ref" in request_schema, (method, path)

    for name, component in schema["components"]["schemas"].items():
        if name in {"HTTPValidationError", "ValidationError"}:
            continue
        if component.get("type") == "object":
            assert component.get("additionalProperties") is False, name
    components = schema["components"]["schemas"]
    assert "archive_routes" not in components["ConfigResponse"]["properties"]
    assert "archive_routes" not in components["ConfigUpdateRequest"][
        "properties"
    ]
    assert "ConfigRouteRequest" not in components
    assert "ConfigRouteResponse" not in components
    assert "library_root" in components["ConfigWatchRequest"]["properties"]
    assert "root" in components["ConfigWatchResponse"]["properties"]
    assert "library_root" in components["ConfigWatchResponse"]["properties"]
    folder_summary = components["FolderObservationSummary"]
    assert "retry_count" in folder_summary["required"]
    assert folder_summary["properties"]["retry_count"] == {
        "maximum": 3.0,
        "minimum": 0.0,
        "title": "Retry Count",
        "type": "integer",
    }


def test_list_read_models_serialize_query_tuples() -> None:
    queries = _Queries()
    queries.list_runs = lambda **_: (  # type: ignore[method-assign]
        {
            "run_id": "run-1",
            "status": "registered",
            "work_type": "anime",
            "created_at": "2026-07-26T00:00:00+00:00",
            "phase": None,
            "plan_hash": None,
            "available_actions": [],
        },
    )
    queries.list_discoveries = lambda **_: (  # type: ignore[method-assign]
        {
            "discovery_id": "discovery-1",
            "watch_id": "watch-1",
            "work_type": "anime",
            "discovered_at": "2026-07-26T00:00:00+00:00",
            "run_id": "run-1",
            "run_status": "registered",
        },
    )
    app = create_api(
        ApiDependencies(queries=queries),
        auth=AuthSettings.create(
            admin_token="admin-token-strong",
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        ),
    )

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://reeloom.test",
            headers={"authorization": "Bearer admin-token-strong"},
        ) as client:
            return (
                await client.get("/api/v1/runs"),
                await client.get("/api/v1/discoveries"),
            )

    runs, discoveries = asyncio.run(scenario())
    assert runs.status_code == 200
    assert discoveries.status_code == 200


def test_admin_auth_host_and_origin_matrix() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            assert (await client.get("/api/v1/runs/run-1")).status_code == 401
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={"authorization": "Bearer old-viewer-token"},
            )).status_code == 401
            assert (await client.get(
                "/api/v1/admin/config",
                headers={"authorization": "Bearer admin-token-strong"},
            )).status_code == 200
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "host": "evil.test",
                },
            )).status_code == 400
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "host": "reeloom.test:garbage",
                },
            )).status_code == 400
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer admin-token-strong",
                    "origin": "https://evil.example",
                },
            )).status_code == 403

    asyncio.run(scenario())


def test_session_bootstrap_reports_role_without_exposing_token() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            admin = await client.get(
                "/api/v1/session",
                headers={"authorization": "Bearer admin-token-strong"},
            )
            rejected = await client.get(
                "/api/v1/session",
                headers={"authorization": "Bearer old-viewer-token"},
            )
            return admin, rejected

    admin, rejected = asyncio.run(scenario())

    assert admin.status_code == 200
    assert admin.json() == {"api_version": "1.0.0", "role": "admin"}
    assert rejected.status_code == 401
    assert "admin-token-strong" not in admin.text


def test_plan_lineage_preview_and_admin_interaction_history() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            admin = {"authorization": "Bearer admin-token-strong"}
            plans = await client.get(
                "/api/v1/runs/run-1/plans",
                headers=admin,
            )
            preview = await client.get(
                "/api/v1/runs/run-1/plans/1/preview",
                headers=admin,
            )
            history = await client.get(
                "/api/v1/runs/run-1/interactions",
                headers=admin,
            )
            return plans, preview, history

    plans, preview, history = asyncio.run(scenario())

    assert plans.status_code == 200
    assert plans.json()["items"][0]["plan_kind"] == "initial"
    assert preview.status_code == 200
    assert preview.json()["items"][0]["source"] == (
        "<script>alert(1)</script>.mkv"
    )
    assert "/absolute/" not in preview.text
    assert history.status_code == 200
    assert history.json()["items"][0]["content_available"] is True


def test_health_fails_closed_without_production_dependency() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            return (
                await client.get("/health"),
                await client.get(
                    "/health",
                    headers={
                        "authorization": "Bearer admin-token-strong"
                    },
                ),
            )

    unauthorized, unavailable = asyncio.run(scenario())

    assert unauthorized.status_code == 401
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {"code": "database_unavailable"}
    }


def test_needs_attention_controls_require_exact_event_head() -> None:
    calls: list[tuple[str, int, str]] = []

    def retry(run_id: str, sequence: int) -> dict[str, object]:
        calls.append((run_id, sequence, "retry"))
        return {
            "run_id": run_id,
            "status": "retry_scheduled",
            "retry_count": 1,
        }

    def fail(run_id: str, sequence: int) -> dict[str, object]:
        calls.append((run_id, sequence, "fail"))
        return {
            "run_id": run_id,
            "status": "failure_planned",
            "plan_hash": "sha256:" + "f" * 64,
        }

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(attention_retry=retry, attention_fail=fail)
            ),
            base_url="http://reeloom.test",
            headers={
                "authorization": "Bearer admin-token-strong",
                "idempotency-key": "attention-control-1",
            },
        ) as client:
            retried = await client.post(
                "/api/v1/runs/run-1/retry",
                headers={"if-match": "event:37"},
                json={},
            )
            failed = await client.post(
                "/api/v1/runs/run-1/fail",
                headers={
                    "if-match": "event:37",
                    "idempotency-key": "attention-control-2",
                },
                json={},
            )
            invalid = await client.post(
                "/api/v1/runs/run-1/retry",
                headers={
                    "if-match": "sha256:" + "a" * 64,
                    "idempotency-key": "attention-control-3",
                },
                json={},
            )
            return retried, failed, invalid

    retried, failed, invalid = asyncio.run(scenario())

    assert retried.status_code == 200
    assert failed.status_code == 200
    assert invalid.status_code == 400
    assert calls == [
        ("run-1", 37, "retry"),
        ("run-1", 37, "fail"),
    ]


def test_needs_attention_question_uses_event_head_without_plan_hash() -> None:
    interactions = _Interactions()

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_app(interactions=interactions)
            ),
            base_url="http://reeloom.test",
            headers={
                "authorization": "Bearer admin-token-strong",
                "idempotency-key": "attention-question-1",
                "if-match": "event:37",
            },
        ) as client:
            return await client.post(
                "/api/v1/runs/run-1/interactions",
                json={"kind": "question", "message": "Why?"},
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert interactions.head == (None, 37)


@pytest.mark.parametrize(
    "token",
    (
        "too-short",
        "admin token with spaces",
        "admin-token-with-emoji-🔑",
        "admin-token-with\nnewline",
    ),
)
def test_auth_rejects_non_header_safe_admin_token(token: str) -> None:
    with pytest.raises(ServerError) as raised:
        AuthSettings.create(
            admin_token=token,
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        )

    assert raised.value.code is ServerErrorCode.INVALID_SETTINGS


def test_auth_environment_requires_only_admin_token() -> None:
    auth = AuthSettings.from_environ(
        {
            "REELOOM_ADMIN_TOKEN": "admin-token-strong",
            "REELOOM_ALLOWED_HOSTS": "reeloom.test",
            "REELOOM_ALLOWED_UI_ORIGINS": "https://ui.example.test",
        }
    )

    assert auth.authenticate("admin-token-strong")
    assert not auth.authenticate("old-viewer-token")


def test_http_and_validation_errors_use_safe_envelopes() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            headers = {
                "authorization": "Bearer admin-token-strong",
            }
            missing = await client.get(
                "/api/v1/runs/missing",
                headers=headers,
            )
            invalid = await client.get(
                "/api/v1/runs",
                params={"limit": "/absolute/private"},
                headers=headers,
            )
            return missing, invalid

    missing, invalid = asyncio.run(scenario())

    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "run_not_found"}}
    assert invalid.status_code == 422
    assert invalid.json() == {"error": {"code": "invalid_request"}}
    assert "/absolute/private" not in invalid.text


def test_duplicate_security_header_is_rejected() -> None:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            return await client.get(
                "/api/v1/runs/run-1",
                headers=[
                    ("authorization", "Bearer admin-token-strong"),
                    ("authorization", "Bearer admin-token-strong"),
                ],
            )

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "duplicate_header"}}


def test_strict_json_rejects_duplicates_and_oversized_body() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            duplicate = await client.post(
            "/api/v1/admin/config/provider-probe",
            content=b'{"revision":1,"revision":2}',
            headers={
                "authorization": "Bearer admin-token-strong",
                "content-type": "application/json",
            },
        )
            oversized = await client.post(
            "/api/v1/admin/config/provider-probe",
            content=json.dumps({"value": "x" * 70_000}).encode(),
            headers={
                "authorization": "Bearer admin-token-strong",
                "content-type": "application/json",
            },
            )
            return duplicate, oversized

    duplicate, oversized = asyncio.run(scenario())

    assert duplicate.status_code == 400
    assert oversized.status_code == 413


def test_sse_is_allowlisted_and_resumes_from_durable_id() -> None:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            return await client.get(
            "/api/v1/runs/run-1/events/stream",
            headers={
                "authorization": "Bearer admin-token-strong",
                "last-event-id": "1",
            },
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "id: 2" in response.text
    assert "event: run_event" in response.text
    assert '"plan_hash"' in response.text
    assert "/absolute/" not in response.text


def test_invalid_or_ahead_sse_cursor_fails_closed() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            invalid = await client.get(
            "/api/v1/runs/run-1/events/stream",
            headers={
                "authorization": "Bearer admin-token-strong",
                "last-event-id": "not-an-int",
            },
        )
            ahead = await client.get(
            "/api/v1/runs/run-1/events/stream",
            headers={
                "authorization": "Bearer admin-token-strong",
                "last-event-id": "999",
            },
            )
            return invalid, ahead

    invalid, ahead = asyncio.run(scenario())

    assert invalid.status_code == 400
    assert ahead.status_code == 409


def test_event_projection_drops_paths_prompts_and_observations() -> None:
    projected = _safe_event(
        "plan_built",
        {
            "plan_hash": "sha256:" + "a" * 64,
            "canonical_plan": '{"source_root":"/absolute/private"}',
            "prompt": "private correction",
            "tool_observation": "untrusted content",
        },
    )

    assert projected == {"plan_hash": "sha256:" + "a" * 64}

    interaction = _safe_event(
        "interaction_completed",
        {
            "interaction_id": "interaction-1",
            "kind": "revision",
            "model_tokens": 12,
            "model_turns": 1,
            "fresh_mapping_submitted": True,
            "plan_hash": "sha256:" + "b" * 64,
            "assistant_reply": "private reply",
        },
    )
    settlement = _safe_event(
        "execution_settled",
        {
            "approval_id": "approval:1",
            "transaction_id": "transaction:1",
            "status": "completed",
            "applied_count": 1,
            "rolled_back_count": 0,
            "failure_code": None,
            "plan_hash": "sha256:" + "b" * 64,
            "journal_path": "/absolute/private",
        },
    )

    assert "assistant_reply" not in interaction
    assert "journal_path" not in settlement
    assert interaction["kind"] == "revision"
    assert settlement["status"] == "completed"

    attention = _safe_event(
        "subtitle_selection_submitted",
        {
            "call_id": "private-call",
            "decision": {
                "reason_code": "subtitle_evidence_ambiguous",
                "selections": [],
                "status": "needs_attention",
            },
        },
    )
    assert attention == {
        "reason_code": "subtitle_evidence_ambiguous",
        "selection_count": 0,
        "status": "needs_attention",
    }
