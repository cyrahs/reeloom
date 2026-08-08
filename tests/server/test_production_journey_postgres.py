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
from reeloom.executor.folder_housekeeping_v2 import (
    housekeeping_target_name,
)
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbMovieDetails,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.server.auth import AuthSettings
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

    async def get_movie(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbMovieDetails:
        assert work_type is TmdbWorkType.MOVIE
        return TmdbMovieDetails(
            tmdb_id=tmdb_id,
            language=language,
            localized_title="旅程电影",
            original_title="Journey Movie",
            release_year=2025,
            original_language="ja",
            adult=False,
            genre_ids=(16,),
            work_type=TmdbWorkType.MOVIE,
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
                name="search_dir",
                arguments={
                    "mode": "selected_tmdb_id",
                    "name": None,
                    "cursor": 0,
                    "limit": 50,
                },
                call_id=f"archive-search-{episode}",
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


def _movie_mapping_model() -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="list-movie",
            ),
            ToolCallStep(
                name="search_tmdb",
                arguments={"query": "Journey Movie", "work_type": "movie"},
                call_id="search-movie",
            ),
            ToolCallStep(
                name="get_tmdb_movie",
                arguments={"tmdb_id": 700, "language": "zh-CN"},
                call_id="details-movie",
            ),
            ToolCallStep(
                name="select_movie",
                arguments={"tmdb_id": 700},
                call_id="select-movie",
            ),
            ToolCallStep(
                name="search_dir",
                arguments={
                    "mode": "selected_tmdb_id",
                    "name": None,
                    "cursor": 0,
                    "limit": 50,
                },
                call_id="archive-search-movie",
            ),
            ToolCallStep(
                name="submit_mapping",
                arguments={"video_id": "video:1", "subtitle_ids": []},
                call_id="mapping-movie",
            ),
        )
    )


@pytest.mark.postgres
def test_production_builder_manual_revision_executes_forward_operation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    for root in (state_root, incoming, archive):
        root.mkdir()
    journey_id = uuid.uuid4().hex
    watch_id = f"journey-watch-{journey_id}"
    source_folder = incoming / "Journey"
    source_folder.mkdir()
    (source_folder / "untrusted.mkv").write_bytes(b"journey-video")
    models = deque(
        (
            _mapping_model(1),
            _mapping_model(2),
        )
    )
    model_lock = threading.Lock()

    def model_factory(config: object, secret: bytes) -> _ModelLease:
        del config
        assert secret == b"offline-provider-key"
        with model_lock:
            return _ModelLease(models.popleft())

    auth = AuthSettings.create(
        admin_token="admin-token-strong",
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
                                "library_root": str(archive),
                                "work_type": "anime",
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
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
                assert public_config["watches"][0]["root"] == str(incoming)
                assert public_config["watches"][0][
                    "library_root"
                ] == str(archive)
                assert (
                    public_config["provider"]["api_key_configured"] is True
                )
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
                                "library_root": {"mode": "retain"},
                                "work_type": "anime",
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
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

                admin_auth = {
                    "authorization": "Bearer admin-token-strong",
                }
                revision = await client.post(
                    f"/api/v1/runs/{run_id}/interactions",
                    headers={
                        **admin_auth,
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
                    headers=admin_auth,
                )
                assert lineage.status_code == 200, lineage.text
                assert [
                    item["plan_hash"]
                    for item in lineage.json()["items"]
                ] == [revised_hash, initial_hash]
                initial_preview = await client.get(
                    f"/api/v1/runs/{run_id}/plans/1/preview?limit=100",
                    headers=admin_auth,
                )
                revised_preview = await client.get(
                    f"/api/v1/runs/{run_id}/plans/2/preview?limit=100",
                    headers=admin_auth,
                )
                assert initial_preview.status_code == 200
                assert revised_preview.status_code == 200
                assert initial_preview.json()["plan_hash"] == initial_hash
                assert revised_preview.json()["plan_hash"] == revised_hash
                assert initial_preview.json()["items"][0]["source"] == (
                    "Journey/untrusted.mkv"
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

                run_before_apply = await client.get(
                    f"/api/v1/runs/{run_id}",
                    headers=admin_auth,
                )
                assert run_before_apply.json()["folder_disposition"] is None
                assert run_before_apply.json()["recovery_approval_id"] is None
                assert "execute" in run_before_apply.json()[
                    "available_actions"
                ]
                applied = await client.post(
                    f"/api/v1/runs/{run_id}/execute",
                    headers={
                        **admin_auth,
                        "if-match": revised_hash,
                    },
                    json={},
                )
                assert applied.status_code == 200, applied.text
                assert applied.json()["status"] == "completed"
                operation_id = applied.json()["operation_id"]
                replayed = await client.post(
                    f"/api/v1/runs/{run_id}/execute",
                    headers={
                        **admin_auth,
                        "if-match": revised_hash,
                    },
                    json={},
                )
                assert replayed.status_code == 200, replayed.text
                assert replayed.json()["operation_id"] == operation_id
                assert replayed.json()["status"] == "completed"

                legacy_apply = await client.post(
                    f"/api/v1/runs/{run_id}/approve-and-apply",
                    headers={
                        **admin_auth,
                        "idempotency-key": f"legacy-apply-{journey_id}",
                        "if-match": revised_hash,
                    },
                    json={
                        "automatic": False,
                        "folder_disposition_plan_hash": None,
                    },
                )
                assert legacy_apply.status_code == 410
                assert legacy_apply.json()["error"]["code"] == (
                    "legacy_effect_superseded"
                )
                legacy_recover = await client.post(
                    f"/api/v1/operations/runs/{run_id}/recover",
                    headers={
                        **admin_auth,
                        "idempotency-key": f"legacy-recover-{journey_id}",
                        "if-match": revised_hash,
                    },
                    json={"approval_id": "approval:legacy"},
                )
                assert legacy_recover.status_code == 410
                assert legacy_recover.json()["error"]["code"] == (
                    "legacy_effect_superseded"
                )
                with application.database.pool.connection() as connection:
                    operation = connection.execute(
                        """
                        SELECT o.status, o.attempt_count,
                               (SELECT count(*) FROM approvals
                                WHERE run_id = %s),
                               (SELECT count(*) FROM approval_claims
                                WHERE run_id = %s),
                               (SELECT count(*) FROM approval_settlements
                                WHERE approval_id IN (
                                    SELECT approval_id FROM approvals
                                    WHERE run_id = %s
                                )),
                               (SELECT count(*)
                                FROM execution_operation_results_v2
                                WHERE operation_id = o.operation_id)
                        FROM execution_operations_v2 AS o
                        WHERE o.operation_id = %s
                        """,
                        (run_id, run_id, run_id, operation_id),
                    ).fetchone()
                assert operation is not None
                assert tuple(operation) == ("completed", 1, 1, 0, 0, 1)
                assert not (source_folder / "untrusted.mkv").exists()
                final = await client.get(
                    f"/api/v1/runs/{run_id}",
                    headers={
                        "authorization": "Bearer admin-token-strong"
                    },
                )
                assert final.json()["status"] == "completed"
                assert final.json()["phase"] == "completed"
                assert final.json()["plan_hash"] == revised_hash
                assert final.json()["settlement"] is None
                assert final.json()["recovery_approval_id"] is None
                assert final.json()["execution"]["status"] == "completed"
                assert not models

        asyncio.run(journey())
    finally:
        application.close()


@pytest.mark.postgres
@pytest.mark.parametrize("movie", (False, True), ids=("anime", "movie"))
def test_production_builder_automatic_policy_uses_exact_approval(
    tmp_path: Path,
    movie: bool,
) -> None:
    state_root = tmp_path / "state"
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    for root in (state_root, incoming, archive):
        root.mkdir()
    source_folder = incoming / "Journey"
    source_folder.mkdir()
    primary = source_folder / "automatic.mkv"
    primary.write_bytes(b"automatic-video")
    if movie:
        (source_folder / "zz-extra.mkv").write_bytes(b"unmapped-extra")
    journey_id = uuid.uuid4().hex
    watch_id = f"automatic-watch-{journey_id}"
    models = deque(
        (
            (_movie_mapping_model(),)
            if movie
            else (_mapping_model(1),)
        )
    )

    def model_factory(config: object, secret: bytes) -> _ModelLease:
        del config
        assert secret == b"offline-provider-key"
        return _ModelLease(models.popleft())

    auth = AuthSettings.create(
        admin_token="admin-token-strong",
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
                                "library_root": str(archive),
                                "work_type": (
                                    "movie" if movie else "anime"
                                ),
                                "poll_interval_seconds": 1,
                                "settle_interval_seconds": 1,
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
                                   count(o.operation_id),
                                   count(result.operation_id)
                            FROM runs AS r
                            JOIN discoveries AS d
                              ON d.discovery_id = r.discovery_id
                            LEFT JOIN plan_heads AS h
                              ON h.run_id = r.run_id
                            LEFT JOIN approvals AS a
                              ON a.run_id = r.run_id
                            LEFT JOIN execution_operations_v2 AS o
                              ON o.run_id = r.run_id
                             AND o.plan_hash = h.plan_hash
                            LEFT JOIN execution_operation_results_v2 AS result
                              ON result.operation_id = o.operation_id
                            WHERE d.watch_id = %s
                            GROUP BY r.run_id, r.status, h.plan_hash
                            """,
                            (watch_id,),
                        ).fetchone()
                    if (
                        row is not None
                        and str(row[1]) == "completed"
                        and (
                            not movie
                            or (
                                incoming
                                / "archive"
                                / housekeeping_target_name(
                                    "Journey", str(row[0])
                                )
                                / "zz-extra.mkv"
                            ).exists()
                        )
                    ):
                        break
                    await asyncio.sleep(0.1)
                assert row is not None
                assert str(row[1]) == "completed"
                assert row[2] is not None
                assert tuple(int(item) for item in row[3:]) == (1, 1, 1)
                assert not primary.exists()
                if movie:
                    assert (
                        incoming
                        / "archive"
                        / housekeeping_target_name(
                            "Journey", str(row[0])
                        )
                        / "zz-extra.mkv"
                    ).exists()
                    movie_root = (
                        archive
                        / "旅程电影 (2025) {tmdb-700}"
                    )
                    target = movie_root / "旅程电影 (2025).mkv"
                    assert target.read_bytes() == b"automatic-video"
                assert not models

        asyncio.run(journey())
    finally:
        application.close()
