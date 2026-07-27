from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
import psycopg
import uvicorn
from agents import ModelSettings
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reeloom.agents.scripted_model import ScriptedModel, ToolCallStep
from reeloom.kernel.tmdb import (
    TmdbLanguage,
    TmdbMovieDetails,
    TmdbSearchCandidate,
    TmdbWorkType,
)
from reeloom.server.auth import AuthSettings, Role
from reeloom.server.composition import ServerApplication, build_application
from reeloom.server.settings import DeploymentSettings

_ADMIN_TOKEN = "admin-e2e-token-strong"


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
        assert work_type is TmdbWorkType.MOVIE
        return (
            TmdbSearchCandidate(
                tmdb_id=700,
                localized_name="旅程电影",
                original_name="Journey Movie",
                year=2025,
                original_language="ja",
                work_type=work_type,
            ),
        )

    async def get_movie(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbMovieDetails:
        assert tmdb_id == 700
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


def _movie_model() -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="list-movie",
            ),
            ToolCallStep(
                name="search_tmdb",
                arguments={
                    "query": "Journey Movie",
                    "work_type": "movie",
                },
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
                name="submit_mapping",
                arguments={"video_id": "video:1", "subtitle_ids": []},
                call_id="mapping-movie",
            ),
        )
    )


async def _seed_movie(
    application: ServerApplication,
    incoming: Path,
    archive: Path,
) -> None:
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application.api),
        base_url="http://127.0.0.1:4173",
    ) as client:
        current = (await client.get("/api/v1/admin/config", headers=headers)).json()
        configured = await client.put(
            "/api/v1/admin/config",
            headers={
                **headers,
                "idempotency-key": "e2e-config-movie",
                "if-match": str(current.get("revision", 0)),
            },
            json={
                "watches": [
                    {
                        "watch_id": "e2e-movie-watch",
                        "root": str(incoming),
                        "work_type": "movie",
                        "poll_interval_seconds": 1,
                        "settle_interval_seconds": 1,
                    }
                ],
                "archive_routes": [
                    {"work_type": "movie", "root": str(archive)}
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
        configured.raise_for_status()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            runs = await client.get(
                "/api/v1/runs?limit=50",
                headers=headers,
            )
            items = runs.json()["items"]
            if any(
                item["work_type"] == "movie"
                and item["status"] == "completed"
                for item in items
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Movie E2E seed did not complete")

        public = configured.json()
        disabled = await client.put(
            "/api/v1/admin/config",
            headers={
                **headers,
                "idempotency-key": "e2e-disable-watch",
                "if-match": str(public["revision"]),
            },
            json={
                "watches": [],
                "archive_routes": [],
                "provider": {
                    "base_url": public["provider"]["base_url"],
                    "model": public["provider"]["model"],
                    "credential": {"mode": "retain"},
                    "reasoning_effort": public["provider"][
                        "reasoning_effort"
                    ],
                    "verbosity": public["provider"]["verbosity"],
                },
                "apply_policy": "automatic",
            },
        )
        disabled.raise_for_status()


def main() -> None:
    dsn = os.environ.get("REELOOM_TEST_POSTGRES_DSN", "")
    if not dsn:
        raise SystemExit("REELOOM_TEST_POSTGRES_DSN must be set explicitly")
    schema = f"reeloom_browser_e2e_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    isolated_dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
    models = deque(_movie_model() for _ in range(8))
    with tempfile.TemporaryDirectory(
        prefix="reeloom-browser-e2e-",
        dir="/private/tmp",
    ) as root:
        state = Path(root) / "state"
        incoming = Path(root) / "incoming"
        archive = Path(root) / "archive"
        for path in (state, incoming, archive):
            path.mkdir()
        (incoming / "automatic.mkv").write_bytes(b"automatic-video")
        (incoming / "zz-extra.mkv").write_bytes(b"unmapped-extra")

        def model_factory(
            config: object,
            secret: bytes,
        ) -> _ModelLease:
            del config
            assert secret == b"offline-provider-key"
            return _ModelLease(models.popleft())

        application = build_application(
            DeploymentSettings(
                postgres_dsn=isolated_dsn,
                state_root=state,
                tmdb_api_key="offline",
            ),
            auth=AuthSettings.create(
                credentials={
                    Role.ADMIN: _ADMIN_TOKEN,
                    Role.OPERATOR: "operator-e2e-token-strong",
                    Role.VIEWER: "viewer-e2e-token-strong",
                },
                allowed_hosts=("127.0.0.1",),
                allowed_origins=("http://127.0.0.1:4173",),
            ),
            model_factory=model_factory,
            tmdb_factory=_TmdbLease,
        )
        try:
            asyncio.run(_seed_movie(application, incoming, archive))
            uvicorn.run(
                application.api,
                host="127.0.0.1",
                port=4173,
                access_log=False,
                server_header=False,
            )
        finally:
            application.close()


if __name__ == "__main__":
    main()
