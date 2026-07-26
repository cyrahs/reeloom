from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
import pytest
from agents import ModelSettings

from reeloom.agents.scripted_model import ScriptedModel, ToolCallStep
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.runtime.event_codec import decode_event
from reeloom.runtime.events import InteractionCompleted
from reeloom.server.auth import AuthSettings, Role
from reeloom.server.composition import build_application
from reeloom.server.settings import DeploymentSettings


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


class _Tmdb:
    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        del query, language, limit, include_adult
        return (
            TmdbSearchCandidate(
                tmdb_id=700,
                localized_name="旅程动画",
                original_name="Journey Anime",
                year=2025,
                original_language="ja",
                work_type=work_type,
            ),
        )

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        return TmdbSeriesDetails(
            tmdb_id=tmdb_id,
            language=language,
            localized_name="旅程动画",
            original_name="Journey Anime",
            first_air_year=2025,
            seasons=(),
            work_type=work_type,
        )

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        return TmdbSeasonDetails(
            tmdb_id=tmdb_id,
            language=language,
            season_number=season_number,
            episodes=tuple(
                TmdbEpisode(
                    season_number=season_number,
                    episode_number=number,
                    name=f"Episode {number}",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                )
                for number in (1, 2)
            ),
            work_type=work_type,
        )


class _TmdbLease:
    provider = _Tmdb()

    async def close(self) -> None:
        return None


class _ModelLease:
    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.model_settings = ModelSettings()

    async def close(self) -> None:
        return None


def _mapping_model(episode: int) -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id=f"list-{episode}",
            ),
            ToolCallStep(
                name="search_tmdb",
                arguments={"query": "Journey Anime", "work_type": "anime"},
                call_id=f"search-{episode}",
            ),
            ToolCallStep(
                name="select_series",
                arguments={"tmdb_id": 700, "work_type": "anime"},
                call_id=f"select-{episode}",
            ),
            ToolCallStep(
                name="get_tmdb_season",
                arguments={
                    "tmdb_id": 700,
                    "work_type": "anime",
                    "season_number": 1,
                    "language": "zh-CN",
                },
                call_id=f"season-{episode}",
            ),
            ToolCallStep(
                name="get_existing_inventory",
                arguments={"tmdb_id": 700},
                call_id=f"inventory-{episode}",
            ),
            ToolCallStep(
                name="submit_mapping",
                arguments={
                    "videos": [
                        {
                            "video_id": "video:1",
                            "season": 1,
                            "episode_start": episode,
                            "episode_end": episode,
                        }
                    ],
                    "subtitles": [],
                },
                call_id=f"mapping-{episode}",
            ),
        )
    )


@pytest.mark.postgres
def test_production_builder_manual_revision_apply_reapply_recover(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    for root in (state_root, incoming, archive):
        root.mkdir()
    journey_id = uuid.uuid4().hex
    watch_id = f"journey-watch-{journey_id}"
    (incoming / "untrusted.mkv").write_bytes(b"journey-video")
    models = deque(
        (
            _mapping_model(1),
            _mapping_model(2),
            _mapping_model(1),
            _mapping_model(2),
            _mapping_model(1),
            _mapping_model(1),
        )
    )
    model_lock = threading.Lock()

    def model_factory(config: object, secret: bytes) -> _ModelLease:
        del config
        assert secret == b"offline-provider-key"
        with model_lock:
            return _ModelLease(models.popleft())

    auth = AuthSettings.create(
        credentials={
            Role.ADMIN: "admin-token-strong",
            Role.OPERATOR: "operator-token-strong",
            Role.VIEWER: "viewer-token-strong",
        },
        allowed_hosts=("reeloom.test",),
        allowed_origins=("https://ui.example.test",),
    )
    application = build_application(
        DeploymentSettings(
            postgres_dsn=_dsn(),
            state_root=state_root,
            tmdb_api_key="offline",
        ),
        auth=auth,
        model_factory=model_factory,
        tmdb_factory=_TmdbLease,
    )
    try:
        transport = httpx.ASGITransport(app=application.api)

        async def journey() -> None:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://reeloom.test",
                timeout=15,
            ) as client:
                admin = {
                    "authorization": "Bearer admin-token-strong",
                    "idempotency-key": f"config-{journey_id}",
                    "if-match": str(
                        (
                            await client.get(
                                "/api/v1/admin/config",
                                headers={
                                    "authorization": (
                                        "Bearer admin-token-strong"
                                    )
                                },
                            )
                        ).json().get("revision", 0)
                    ),
                }
                response = await client.put(
                    "/api/v1/admin/config",
                    headers=admin,
                    json={
                        "watches": [
                            {
                                "watch_id": watch_id,
                                "root": str(incoming),
                                "work_type": "anime",
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
                            }
                        ],
                        "archive_routes": [
                            {
                                "work_type": "anime",
                                "root": str(archive),
                            }
                        ],
                        "provider": {
                            "base_url": "https://api.openai.com/v1",
                            "model": "gpt-offline",
                            "api_key": "offline-provider-key",
                            "reasoning_effort": None,
                            "verbosity": None,
                        },
                        "apply_policy": "manual",
                    },
                )
                assert response.status_code == 200, response.text
                public_config = response.json()
                assert public_config["watches"][0]["root_configured"] is True
                assert (
                    public_config["provider"]["api_key_configured"] is True
                )
                assert str(incoming) not in response.text
                assert "offline-provider-key" not in response.text
                retained = await client.put(
                    "/api/v1/admin/config",
                    headers={
                        "authorization": "Bearer admin-token-strong",
                        "idempotency-key": f"retain-{journey_id}",
                        "if-match": str(public_config["revision"]),
                    },
                    json={
                        "watches": [
                            {
                                "watch_id": watch_id,
                                "root": {"mode": "retain"},
                                "work_type": "anime",
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
                            }
                        ],
                        "archive_routes": [
                            {
                                "work_type": "anime",
                                "root": {"mode": "retain"},
                            }
                        ],
                        "provider": {
                            "base_url": "https://api.openai.com/v1",
                            "model": "gpt-offline",
                            "credential": {"mode": "retain"},
                            "reasoning_effort": None,
                            "verbosity": None,
                        },
                        "apply_policy": "manual",
                    },
                )
                assert retained.status_code == 200, retained.text
                assert retained.json()["revision"] == (
                    public_config["revision"] + 1
                )

                deadline = time.monotonic() + 8
                run_id = None
                initial_hash = None
                while time.monotonic() < deadline:
                    with application.database.pool.connection() as connection:
                        row = connection.execute(
                            """
                            SELECT r.run_id, h.plan_hash
                            FROM runs AS r
                            JOIN discoveries AS d
                              ON d.discovery_id = r.discovery_id
                            LEFT JOIN plan_heads AS h
                              ON h.run_id = r.run_id
                            WHERE d.watch_id = %s
                            ORDER BY r.created_at DESC
                            LIMIT 1
                            """,
                            (watch_id,),
                        ).fetchone()
                    if row is not None and row[1] is not None:
                        run_id, initial_hash = str(row[0]), str(row[1])
                        break
                    await asyncio.sleep(0.1)
                assert run_id is not None
                assert initial_hash is not None

                operator = {
                    "authorization": "Bearer operator-token-strong",
                }
                revision = await client.post(
                    f"/api/v1/runs/{run_id}/interactions",
                    headers={
                        **operator,
                        "idempotency-key": f"revision-{journey_id}",
                        "if-match": initial_hash,
                    },
                    json={
                        "kind": "revision",
                        "message": "Map the complete set to episode 2.",
                    },
                )
                assert revision.status_code == 200, revision.text
                revised_hash = revision.json()["plan_hash"]
                assert revised_hash != initial_hash
                session = await client.get(
                    "/api/v1/session",
                    headers={
                        "authorization": "Bearer admin-token-strong"
                    },
                )
                assert session.json() == {
                    "api_version": "1.0.0",
                    "role": "admin",
                }
                lineage = await client.get(
                    f"/api/v1/runs/{run_id}/plans?limit=100",
                    headers=operator,
                )
                assert lineage.status_code == 200, lineage.text
                assert [
                    item["plan_hash"]
                    for item in lineage.json()["items"]
                ] == [revised_hash, initial_hash]
                initial_preview = await client.get(
                    f"/api/v1/runs/{run_id}/plans/1/preview?limit=100",
                    headers=operator,
                )
                revised_preview = await client.get(
                    f"/api/v1/runs/{run_id}/plans/2/preview?limit=100",
                    headers=operator,
                )
                assert initial_preview.status_code == 200
                assert revised_preview.status_code == 200
                assert initial_preview.json()["plan_hash"] == initial_hash
                assert revised_preview.json()["plan_hash"] == revised_hash
                assert initial_preview.json()["items"][0]["source"] == (
                    "untrusted.mkv"
                )
                assert str(incoming) not in initial_preview.text
                history = await client.get(
                    f"/api/v1/runs/{run_id}/interactions?limit=100",
                    headers={
                        "authorization": "Bearer admin-token-strong"
                    },
                )
                assert history.status_code == 200, history.text
                assert history.json()["items"][0]["request_message"] == (
                    "Map the complete set to episode 2."
                )
                assert history.json()["items"][0][
                    "content_available"
                ] is True
                forbidden_history = await client.get(
                    f"/api/v1/runs/{run_id}/interactions",
                    headers=operator,
                )
                assert forbidden_history.status_code == 403

                applied = await client.post(
                    f"/api/v1/runs/{run_id}/approve-and-apply",
                    headers={
                        **operator,
                        "idempotency-key": f"apply-revision-{journey_id}",
                        "if-match": revised_hash,
                    },
                    json={"automatic": False},
                )
                assert applied.status_code == 200, applied.text
                assert applied.json()["status"] == "completed"
                assert not (incoming / "untrusted.mkv").exists()

                reapplied = await client.post(
                    f"/api/v1/runs/{run_id}/reapply",
                    headers={
                        **operator,
                        "idempotency-key": f"reapply-{journey_id}",
                        "if-match": revised_hash,
                    },
                    json={"message": "Map the complete set to episode 1."},
                )
                assert reapplied.status_code == 200, reapplied.text
                amendment_hash = reapplied.json()["plan_hash"]
                assert amendment_hash is not None

                superseded = await client.post(
                    f"/api/v1/runs/{run_id}/reapply",
                    headers={
                        **operator,
                        "idempotency-key": f"supersede-{journey_id}",
                        "if-match": amendment_hash,
                    },
                    json={
                        "message": (
                            "Freshly restore the current complete episode 2 "
                            "mapping."
                        )
                    },
                )
                assert superseded.status_code == 200, superseded.text
                assert superseded.json()["no_op"] is True
                assert superseded.json()["plan_hash"] is None
                with application.database.pool.connection() as connection:
                    projection = connection.execute(
                        """
                        SELECT h.plan_hash, s.phase, s.plan_hash, r.status
                        FROM plan_heads AS h
                        JOIN run_states AS s USING (run_id)
                        JOIN runs AS r USING (run_id)
                        WHERE h.run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                    stored_event = connection.execute(
                        """
                        SELECT payload FROM run_events
                        WHERE run_id = %s
                        ORDER BY sequence DESC
                        LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                assert tuple(str(item) for item in projection) == (
                    revised_hash,
                    "completed",
                    revised_hash,
                    "completed",
                )
                event = decode_event(bytes(stored_event[0]))
                assert isinstance(event, InteractionCompleted)
                assert event.plan_hash is None
                assert event.final_plan_hash == revised_hash

                reapplied = await client.post(
                    f"/api/v1/runs/{run_id}/reapply",
                    headers={
                        **operator,
                        "idempotency-key": f"reapply-final-{journey_id}",
                        "if-match": revised_hash,
                    },
                    json={"message": "Map the complete set to episode 1."},
                )
                assert reapplied.status_code == 200, reapplied.text
                amendment_hash = reapplied.json()["plan_hash"]
                assert amendment_hash is not None

                pending_amendment = await client.get(
                    f"/api/v1/runs/{run_id}",
                    headers={
                        "authorization": "Bearer viewer-token-strong"
                    },
                )
                assert pending_amendment.status_code == 200
                assert pending_amendment.json()["plan_hash"] == amendment_hash
                assert pending_amendment.json()["settlement"] is None

                amendment = await client.post(
                    f"/api/v1/runs/{run_id}/approve-and-apply",
                    headers={
                        **operator,
                        "idempotency-key": f"apply-amend-{journey_id}",
                        "if-match": amendment_hash,
                    },
                    json={"automatic": False},
                )
                assert amendment.status_code == 200, amendment.text
                assert amendment.json()["status"] == "completed"

                recovered = await client.post(
                    f"/api/v1/operations/runs/{run_id}/recover",
                    headers={
                        **operator,
                        "idempotency-key": f"recover-{journey_id}",
                        "if-match": amendment_hash,
                    },
                    json={
                        "approval_id": amendment.json()["approval_id"]
                    },
                )
                assert recovered.status_code == 200, recovered.text
                assert (
                    recovered.json()["transaction_id"]
                    == amendment.json()["transaction_id"]
                )
                no_op = await client.post(
                    f"/api/v1/runs/{run_id}/reapply",
                    headers={
                        **operator,
                        "idempotency-key": f"noop-{journey_id}",
                        "if-match": amendment_hash,
                    },
                    json={"message": "Revalidate the current full mapping."},
                )
                assert no_op.status_code == 200, no_op.text
                assert no_op.json()["no_op"] is True
                assert no_op.json()["plan_hash"] is None
                with application.database.pool.connection() as connection:
                    counts = connection.execute(
                        """
                        SELECT
                            (SELECT count(*) FROM plan_lineage
                             WHERE run_id = %s),
                            (SELECT count(*) FROM approvals
                             WHERE run_id = %s)
                        """,
                        (run_id, run_id),
                    ).fetchone()
                    final_projection = connection.execute(
                        """
                        SELECT phase, plan_hash
                        FROM run_states
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                assert tuple(int(item) for item in counts) == (4, 2)
                assert tuple(str(item) for item in final_projection) == (
                    "completed",
                    amendment_hash,
                )
                final = await client.get(
                    f"/api/v1/runs/{run_id}",
                    headers={
                        "authorization": "Bearer viewer-token-strong"
                    },
                )
                assert final.json()["status"] == "completed"
                assert final.json()["phase"] == "completed"
                assert final.json()["plan_hash"] == amendment_hash
                assert final.json()["settlement"]["transaction_id"] == (
                    amendment.json()["transaction_id"]
                )
                assert final.json()["settlement"]["status"] == "completed"
                assert not models

        asyncio.run(journey())
    finally:
        application.close()


@pytest.mark.postgres
def test_production_builder_automatic_policy_uses_exact_approval(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    for root in (state_root, incoming, archive):
        root.mkdir()
    (incoming / "automatic.mkv").write_bytes(b"automatic-video")
    journey_id = uuid.uuid4().hex
    watch_id = f"automatic-watch-{journey_id}"
    models = deque((_mapping_model(1),))

    def model_factory(config: object, secret: bytes) -> _ModelLease:
        del config
        assert secret == b"offline-provider-key"
        return _ModelLease(models.popleft())

    auth = AuthSettings.create(
        credentials={
            Role.ADMIN: "admin-token-strong",
            Role.OPERATOR: "operator-token-strong",
            Role.VIEWER: "viewer-token-strong",
        },
        allowed_hosts=("reeloom.test",),
        allowed_origins=("https://ui.example.test",),
    )
    application = build_application(
        DeploymentSettings(
            postgres_dsn=_dsn(),
            state_root=state_root,
            tmdb_api_key="offline",
        ),
        auth=auth,
        model_factory=model_factory,
        tmdb_factory=_TmdbLease,
    )
    try:
        async def journey() -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application.api),
                base_url="http://reeloom.test",
                timeout=15,
            ) as client:
                config_response = await client.get(
                    "/api/v1/admin/config",
                    headers={
                        "authorization": "Bearer admin-token-strong"
                    },
                )
                expected = config_response.json().get("revision", 0)
                updated = await client.put(
                    "/api/v1/admin/config",
                    headers={
                        "authorization": "Bearer admin-token-strong",
                        "idempotency-key": f"config-{journey_id}",
                        "if-match": str(expected),
                    },
                    json={
                        "watches": [
                            {
                                "watch_id": watch_id,
                                "root": str(incoming),
                                "work_type": "anime",
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
                            }
                        ],
                        "archive_routes": [
                            {
                                "work_type": "anime",
                                "root": str(archive),
                            }
                        ],
                        "provider": {
                            "base_url": "https://api.openai.com/v1",
                            "model": "gpt-offline",
                            "api_key": "offline-provider-key",
                            "reasoning_effort": None,
                            "verbosity": None,
                        },
                        "apply_policy": "automatic",
                    },
                )
                assert updated.status_code == 200, updated.text

                deadline = time.monotonic() + 8
                row = None
                while time.monotonic() < deadline:
                    with application.database.pool.connection() as connection:
                        row = connection.execute(
                            """
                            SELECT r.run_id, r.status, h.plan_hash,
                                   count(a.approval_id),
                                   count(c.approval_id),
                                   count(s.approval_id)
                            FROM runs AS r
                            JOIN discoveries AS d
                              ON d.discovery_id = r.discovery_id
                            LEFT JOIN plan_heads AS h
                              ON h.run_id = r.run_id
                            LEFT JOIN approvals AS a
                              ON a.run_id = r.run_id
                            LEFT JOIN approval_claims AS c
                              ON c.approval_id = a.approval_id
                            LEFT JOIN approval_settlements AS s
                              ON s.approval_id = a.approval_id
                            WHERE d.watch_id = %s
                            GROUP BY r.run_id, r.status, h.plan_hash
                            """,
                            (watch_id,),
                        ).fetchone()
                    if row is not None and str(row[1]) == "completed":
                        break
                    await asyncio.sleep(0.1)
                assert row is not None
                assert str(row[1]) == "completed"
                assert row[2] is not None
                assert tuple(int(item) for item in row[3:]) == (1, 1, 1)
                assert not (incoming / "automatic.mkv").exists()
                assert not models

        asyncio.run(journey())
    finally:
        application.close()
