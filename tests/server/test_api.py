from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass

import httpx
import pytest

from reeloom.server.api import ApiDependencies, create_api
from reeloom.server.auth import AuthSettings, Role
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.queries import _safe_event


@dataclass
class _Queries:
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
            "phase": "awaiting_approval",
            "plan_hash": "sha256:" + "a" * 64,
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
            "watches": [{"watch_id": "w", "work_type": "anime"}],
            "provider": {"model": "gpt-5"},
        }

    def get_plan(
        self,
        *,
        run_id: str,
        version: int | None,
    ) -> dict[str, object] | None:
        del version
        return (
            {"run_id": run_id, "plan_hash": "sha256:" + "a" * 64}
            if run_id == "run-1"
            else None
        )


def _app() -> object:
    app = create_api(
        ApiDependencies(queries=_Queries()),
        auth=AuthSettings.create(
            credentials={
                Role.ADMIN: "admin-token-strong",
                Role.OPERATOR: "operator-token-strong",
                Role.VIEWER: "viewer-token-strong",
            },
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        ),
    )
    return app


def test_auth_role_host_and_origin_matrix() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            assert (await client.get("/api/v1/runs/run-1")).status_code == 401
            assert (await client.get(
            "/api/v1/runs/run-1",
            headers={"authorization": "Bearer viewer-token-strong"},
        )).status_code == 200
            assert (await client.get(
            "/api/v1/admin/config",
            headers={"authorization": "Bearer operator-token-strong"},
        )).status_code == 403
            assert (await client.get(
            "/api/v1/admin/config",
            headers={"authorization": "Bearer admin-token-strong"},
        )).status_code == 200
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer viewer-token-strong",
                    "host": "evil.test",
                },
            )).status_code == 400
            assert (await client.get(
                "/api/v1/runs/run-1",
                headers={
                    "authorization": "Bearer viewer-token-strong",
                    "host": "reeloom.test:garbage",
                },
            )).status_code == 400
            assert (await client.get(
            "/api/v1/runs/run-1",
            headers={
                "authorization": "Bearer viewer-token-strong",
                "origin": "https://evil.example",
            },
        )).status_code == 403

    asyncio.run(scenario())


def test_health_fails_closed_without_production_dependency() -> None:
    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "database_unavailable"}
    }


def test_auth_rejects_credentials_shared_across_roles() -> None:
    with pytest.raises(ServerError) as raised:
        AuthSettings.create(
            credentials={
                Role.ADMIN: "shared-token-strong",
                Role.OPERATOR: "shared-token-strong",
                Role.VIEWER: "viewer-token-strong",
            },
            allowed_hosts=("reeloom.test",),
            allowed_origins=("https://ui.example.test",),
        )

    assert raised.value.code is ServerErrorCode.INVALID_SETTINGS


def test_http_and_validation_errors_use_safe_envelopes() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()),
            base_url="http://reeloom.test",
        ) as client:
            headers = {
                "authorization": "Bearer viewer-token-strong",
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
                    ("authorization", "Bearer viewer-token-strong"),
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
                "authorization": "Bearer viewer-token-strong",
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
                "authorization": "Bearer viewer-token-strong",
                "last-event-id": "not-an-int",
            },
        )
            ahead = await client.get(
            "/api/v1/runs/run-1/events/stream",
            headers={
                "authorization": "Bearer viewer-token-strong",
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
