from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents import ModelSettings

from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.agents.organizer import EPISODE_ORGANIZER_INSTRUCTIONS
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
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraftInput,
    ProviderConfigInput,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.config_service import ConfigService
from reeloom.server.database import PostgresControlPlane
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.organizer_definition import (
    LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    ORGANIZER_NAME,
)
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.session import PostgresSessionRepository
from reeloom.server.watcher import NoFollowWatcher


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
                tmdb_id=200,
                localized_name="测试动画",
                original_name="Test Anime",
                year=2024,
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
            localized_name="测试动画",
            original_name="Test Anime",
            first_air_year=2024,
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
            episodes=(
                TmdbEpisode(
                    season_number=season_number,
                    episode_number=1,
                    name="第一集",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
            ),
            work_type=work_type,
        )


class _TmdbLease:
    provider = _Tmdb()
    closed = False

    async def close(self) -> None:
        self.closed = True


class _ModelLease:
    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.model_settings = ModelSettings()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _model() -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="list",
            ),
            ToolCallStep(
                name="search_tmdb",
                arguments={"query": "Test Anime", "work_type": "anime"},
                call_id="search",
            ),
            ToolCallStep(
                name="select_series",
                arguments={"tmdb_id": 200, "work_type": "anime"},
                call_id="select",
            ),
            ToolCallStep(
                name="get_tmdb_season",
                arguments={
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 1,
                    "language": "zh-CN",
                },
                call_id="season",
            ),
            ToolCallStep(
                name="search_dir",
                arguments={
                    "mode": "selected_tmdb_id",
                    "name": None,
                    "cursor": 0,
                    "limit": 50,
                },
                call_id="archive-search",
            ),
            ToolCallStep(
                name="submit_mapping",
                arguments={
                    "videos": [
                        {
                            "video_id": "video:1",
                            "season": 1,
                            "episode_start": 1,
                            "episode_end": 1,
                        }
                    ],
                    "subtitles": [],
                    "review": {
                        "summary": "唯一视频已映射为 S01E01。",
                        "unmapped_explanations": [],
                    },
                },
                call_id="mapping",
            ),
        )
    )


@pytest.mark.postgres
def test_initial_worker_runs_real_sdk_loop_and_resumes_identity(
    tmp_path: Path,
) -> None:
    control = PostgresControlPlane(_dsn())
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    secret_root = tmp_path / "secrets"
    plan_root = tmp_path / "plans"
    for root in (incoming, archive, secret_root, plan_root):
        root.mkdir()
    (incoming / "untrusted S01E01.mkv").write_bytes(b"video")
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        previous = configs.head()
        expected = 0 if previous is None else previous.revision
        secrets = FilesystemSecretStore(
            AuthorizedRoot.create(secret_root)
        )
        revision = ConfigService(
            configs=configs,
            secrets=secrets,
        ).compare_and_append(
            expected_revision=expected,
            value=ConfigDraftInput(
                watches=(
                    WatchConfig(
                        watch_id=f"watch-{uuid.uuid4().hex}",
                        root=incoming,
                        library_root=archive,
                        work_type=ServerWorkType.ANIME,
                        poll_interval_seconds=1,
                        settle_interval_seconds=1,
                    ),
                ),
                provider=ProviderConfigInput(
                    base_url="https://api.openai.com/v1",
                    model="gpt-test",
                    api_key=b"not-a-real-secret",
                ),
                apply_policy=ApplyPolicy.MANUAL,
                agent_budget=RunBudget(
                    max_elapsed_seconds=321,
                ),
            ),
        )
        scheduler = PostgresSchedulerRepository(control.pool)
        watch = revision.watches[0]
        scheduler.configure_watch(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            work_type=watch.work_type,
            settle_interval_seconds=1,
        )
        snapshot = NoFollowWatcher().scan(
            AuthorizedRoot.create(incoming)
        )
        observed = datetime.now(UTC)
        scheduler.reconcile_poll(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed,
            snapshot=snapshot,
        )
        settled = scheduler.reconcile_poll(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed + timedelta(seconds=1),
            snapshot=snapshot,
        )
        assert settled.discovery is not None
        registration = scheduler.register_run(
            discovery_id=settled.discovery.discovery_id
        )
        model = _model()
        model_lease = _ModelLease(model)
        tmdb_lease = _TmdbLease()
        plans = FilesystemPlanStore(
            AuthorizedRoot.create(plan_root)
        )
        worker = InitialAgentWorker(
            scheduler=scheduler,
            configs=configs,
            definitions=PostgresAgentDefinitionRepository(control.pool),
            sessions=PostgresSessionRepository(control.pool),
            secrets=secrets,
            plans=plans,
            model_factory=lambda config, secret: (
                model_lease
                if (
                    config.revision == revision.revision
                    and secret == b"not-a-real-secret"
                )
                else (_ for _ in ()).throw(AssertionError())
            ),
            tmdb_factory=lambda: tmdb_lease,
            pool=control.pool,
        )

        plan_hash = asyncio.run(worker.run(run_id=registration.run_id))
        recovered_hash = asyncio.run(worker.run(run_id=registration.run_id))

        assert recovered_hash == plan_hash
        assert model.exhausted
        assert model_lease.closed
        assert tmdb_lease.closed
        assert plans.load(plan_hash)
        state = PostgresEventStore(
            control.pool,
            run_id=registration.run_id,
            plans=plans,
        ).state
        assert state is not None
        assert state.plan_hash == plan_hash
        assert state.budget.max_elapsed_seconds == 321
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.agent_definition_hash, r.session_id, s.revision,
                       d.payload->>'instructions',
                       d.payload->>'schema_version', review.payload
                FROM runs AS r
                JOIN agent_sessions AS s ON s.run_id = r.run_id
                JOIN agent_definitions AS d
                  ON d.definition_hash = r.agent_definition_hash
                JOIN plan_reviews AS review
                  ON review.run_id = r.run_id
                 AND review.plan_hash = %s
                WHERE r.run_id = %s
                """,
                (plan_hash, registration.run_id),
            ).fetchone()
        assert row is not None
        assert str(row[1]) == registration.run_id
        assert int(row[2]) > 0
        assert EPISODE_ORGANIZER_INSTRUCTIONS in str(row[3])
        assert row[4] == "episode-organizer-v4"
        assert row[5]["status"] == "agent_and_system"
        assert row[5]["agent_summary"] == "唯一视频已映射为 S01E01。"

        legacy = AgentDefinitionRevision.create(
            name=ORGANIZER_NAME,
            instructions="Historical v1 organizer.",
            tools=LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
            schema_version=LEGACY_ORGANIZER_SCHEMA_VERSION,
        )
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO agent_definitions
                        (definition_hash, payload)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (definition_hash) DO NOTHING
                    """,
                    (legacy.definition_hash, legacy.to_json()),
                )
                connection.execute(
                    """
                    UPDATE runs
                    SET agent_definition_hash = %s
                    WHERE run_id = %s
                    """,
                    (legacy.definition_hash, registration.run_id),
                )

        assert (
            asyncio.run(worker.run(run_id=registration.run_id))
            == plan_hash
        )
    finally:
        control.close()
