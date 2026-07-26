from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest

from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    ModelUsageRecorded,
    RunStarted,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.session import PostgresSessionRepository, RepositoryAgentSession


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_runtime_projection_and_session_survive_restart() -> None:
    control = PostgresControlPlane(_dsn())
    run_id = f"runtime-{uuid.uuid4().hex}"
    discovery_id = f"discovery-{uuid.uuid4().hex}"
    try:
        control.open()
        control.migrate()
        config_repository = PostgresConfigRepository(control.pool)
        config_head = config_repository.head()
        if config_head is None:
            config_head = config_repository.compare_and_append(
                expected_revision=0,
                revision=ConfigRevision.create(
                    revision_id=f"cfg-{uuid.uuid4().hex}",
                    revision=1,
                    created_at=datetime.now(UTC),
                    draft=ConfigDraft(
                        watches=(),
                        archive_routes=(),
                        provider=ProviderConfig(
                            base_url="https://api.openai.com/v1",
                            model="gpt-5",
                            secret_ref="secret-test",
                        ),
                        apply_policy=ApplyPolicy.MANUAL,
                    ),
                ),
            )
        watch_id = f"watch-{uuid.uuid4().hex}"
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, 1, 'anime', 1)
                    """,
                    (watch_id, config_head.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision, snapshot_id,
                         snapshot_payload, work_type, discovered_at)
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime', %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config_head.revision,
                        f"snapshot-{uuid.uuid4().hex}",
                        datetime.now(UTC),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'registered')
                    """,
                    (
                        run_id,
                        discovery_id,
                        config_head.revision,
                        f"cap-{uuid.uuid4().hex}",
                    ),
                )
        store = PostgresEventStore(control.pool, run_id=run_id)
        store.append(
            RunStarted(run_id, TmdbWorkType.ANIME, RunBudget())
        )
        store.append(CandidateSnapshotCreated("snapshot:test", 0))
        store.append(ModelUsageRecorded(2, 3, 5))

        recovered = PostgresEventStore(control.pool, run_id=run_id)
        assert recovered.state == store.state
        assert recovered.state is not None
        assert recovered.state.model_tokens == 5

        async def session_scenario() -> None:
            repository = PostgresSessionRepository(control.pool)
            session = RepositoryAgentSession(
                repository=repository,
                run_id=run_id,
                session_id=run_id,
            )
            await session.add_items([{"role": "user", "content": "hello"}])
            restarted = RepositoryAgentSession(
                repository=repository,
                run_id=run_id,
                session_id=run_id,
            )
            assert await restarted.get_items() == [
                {"role": "user", "content": "hello"}
            ]

        asyncio.run(session_scenario())
    finally:
        control.close()
