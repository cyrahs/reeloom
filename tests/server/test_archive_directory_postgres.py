from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.events import RunFailed, RunStarted
from reeloom.runtime.state import RunStatus
from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.composition import _retire_legacy_folder_runs
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.organizer_definition import (
    LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    ORGANIZER_NAME,
)
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import NoFollowWatcher


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_retired_v1_folder_run_restarts_without_source_side_effect(
    tmp_path: Path,
) -> None:
    control = PostgresControlPlane(_dsn())
    watch_root = tmp_path / "watch"
    plan_root = tmp_path / "plans"
    source = watch_root / "Incoming" / "Episode S01E01.mkv"
    source.parent.mkdir(parents=True)
    plan_root.mkdir()
    source.write_bytes(b"video")
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        head = configs.head()
        expected = 0 if head is None else head.revision
        config = ConfigRevision.create(
            revision_id=f"cfg-{uuid.uuid4().hex}",
            revision=expected + 1,
            created_at=datetime.now(UTC),
            draft=ConfigDraft(
                watches=(),
                provider=ProviderConfig(
                    base_url="https://api.openai.com/v1",
                    model="test",
                    secret_ref="secret-test",
                ),
                apply_policy=ApplyPolicy.MANUAL,
            ),
        )
        configs.compare_and_append(
            expected_revision=expected,
            revision=config,
        )
        scheduler = PostgresSchedulerRepository(control.pool)
        watch_id = f"watch-{uuid.uuid4().hex}"
        scheduler.configure_watch(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            work_type=ServerWorkType.ANIME,
            settle_interval_seconds=1,
        )
        watcher = NoFollowWatcher()
        started = datetime.now(UTC)
        scan = watcher.scan_folders(
            AuthorizedRoot.create(watch_root)
        )
        scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started,
            scan=scan,
        )
        first = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started + timedelta(seconds=1),
            scan=scan,
        ).discoveries[0]
        registration = scheduler.register_run(
            discovery_id=first.discovery_id
        )
        legacy = AgentDefinitionRevision.create(
            name=ORGANIZER_NAME,
            instructions="Historical v1 organizer.",
            tools=LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
            schema_version=LEGACY_ORGANIZER_SCHEMA_VERSION,
        )
        PostgresAgentDefinitionRepository(
            control.pool
        ).register_and_bind(
            run_id=registration.run_id,
            definition=legacy,
            session_id=registration.run_id,
        )
        plans = FilesystemPlanStore(
            AuthorizedRoot.create(plan_root)
        )
        store = PostgresEventStore(
            control.pool,
            run_id=registration.run_id,
            plans=plans,
        )
        store.append(
            RunStarted(
                registration.run_id,
                TmdbWorkType.ANIME,
            )
        )
        old_boot = f"boot-{uuid.uuid4().hex}"
        control.register_boot(old_boot)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', boot_id = %s
                    WHERE job_id = %s
                    """,
                    (old_boot, registration.job_id),
                )
        scheduler.reconcile_boot(current_boot_id="boot-new")

        assert scheduler.legacy_active_folder_runs(
            schema_versions=(LEGACY_ORGANIZER_SCHEMA_VERSION,)
        ) == (registration.run_id,)
        store.append(RunFailed(code="retired_tool_call"))
        assert scheduler.legacy_active_folder_runs(
            schema_versions=(LEGACY_ORGANIZER_SCHEMA_VERSION,)
        ) == (registration.run_id,)
        _retire_legacy_folder_runs(
            database=control,
            plans=plans,
            scheduler=scheduler,
        )

        retired = PostgresEventStore(
            control.pool,
            run_id=registration.run_id,
            plans=plans,
        ).state
        assert retired is not None
        assert retired.status is RunStatus.FAILED
        assert retired.failure_code == "retired_tool_call"
        assert source.read_bytes() == b"video"
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT job.status, observed.status,
                       observed.discovery_id
                FROM jobs AS job
                JOIN runs AS run ON run.run_id = job.run_id
                JOIN discoveries AS discovery
                  ON discovery.discovery_id = run.discovery_id
                JOIN watch_folder_observations AS observed
                  ON observed.watch_id = discovery.watch_id
                 AND observed.folder_name = discovery.source_folder
                WHERE run.run_id = %s
                """,
                (registration.run_id,),
            ).fetchone()
        assert tuple(row) == ("failed", "settling", None)

        restarted_at = datetime.now(UTC) + timedelta(seconds=2)
        restarted = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=restarted_at,
            scan=watcher.scan_folders(
                AuthorizedRoot.create(watch_root)
            ),
        )
        assert len(restarted.discoveries) == 1
        assert (
            restarted.discoveries[0].folder_generation_id
            != first.folder_generation_id
        )
        assert source.read_bytes() == b"video"
    finally:
        control.close()
