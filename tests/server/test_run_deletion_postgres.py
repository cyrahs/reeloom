from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.queries import PostgresQueries
from reeloom.server.run_deletion import PostgresRunDeletionService
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import FolderScan


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def _ensure_config(
    control: PostgresControlPlane, suffix: str
) -> ConfigRevision:
    repository = PostgresConfigRepository(control.pool)
    config = repository.head()
    if config is not None:
        return config
    return repository.compare_and_append(
        expected_revision=0,
        revision=ConfigRevision.create(
            revision_id=f"config-{suffix}",
            revision=1,
            created_at=datetime.now(UTC),
            draft=ConfigDraft(
                watches=(),
                provider=ProviderConfig(
                    base_url="https://api.openai.com/v1",
                    model="gpt-5",
                    secret_ref="secret-test",
                ),
                apply_policy=ApplyPolicy.MANUAL,
            ),
        ),
    )


@pytest.mark.postgres
def test_terminal_run_can_be_hidden_without_deleting_history() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-delete-{suffix}"
    discovery_id = f"discovery-delete-{suffix}"
    run_id = f"run-delete-{suffix}"
    try:
        control.open()
        control.migrate()
        config = _ensure_config(control, suffix)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (
                        %s, %s, %s, %s,
                        '{"files":[],"snapshot_id":"empty"}'::jsonb,
                        'anime', clock_timestamp()
                    )
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{suffix}",
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
                        config.revision,
                        f"source-{suffix}",
                    ),
                )

        deletion = PostgresRunDeletionService(control.pool)
        with pytest.raises(ServerError) as error:
            deletion.delete(run_id)
        assert error.value.code is ServerErrorCode.RUN_DELETE_CONFLICT

        with control.pool.connection() as connection:
            connection.execute(
                "UPDATE runs SET status = 'completed' WHERE run_id = %s",
                (run_id,),
            )

        queries = PostgresQueries(control.pool)
        run = queries.get_run(run_id)
        assert run is not None
        assert "delete_run" in run["available_actions"]

        result = deletion.delete(run_id)
        assert deletion.delete(run_id) == result
        assert queries.get_run(run_id) is None
        assert not queries.is_run_visible(run_id)
        assert run_id not in {
            item["run_id"]
            for item in queries.list_runs(before=None, limit=10_000)
        }
        discovery = next(
            item
            for item in queries.list_discoveries(
                before=None, limit=10_000
            )
            if item["discovery_id"] == discovery_id
        )
        assert discovery["run_id"] is None

        with control.pool.connection() as connection:
            operation = connection.execute(
                """
                SELECT operation_kind
                FROM run_operations
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
            persisted_run = connection.execute(
                "SELECT status FROM runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        assert operation == ("delete",)
        assert persisted_run == ("completed",)
    finally:
        control.close()


@pytest.mark.postgres
def test_folder_generation_retry_count_is_durable_and_bounded() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-retry-{suffix}"
    discovery_id = f"discovery-retry-{suffix}"
    run_id = f"run-retry-{suffix}"
    started = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        config = _ensure_config(control, suffix)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at, source_folder,
                         folder_generation_id, inventory_id)
                    VALUES (
                        %s, %s, %s, %s,
                        '{"files":[],"snapshot_id":"empty"}'::jsonb,
                        'anime', %s, 'retry-folder', %s, %s
                    )
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{suffix}",
                        started,
                        f"generation-{suffix}",
                        f"inventory-{suffix}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'running')
                    """,
                    (
                        run_id,
                        discovery_id,
                        config.revision,
                        f"source-{suffix}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO jobs
                        (job_id, run_id, status, updated_at)
                    VALUES (%s, %s, 'running', %s)
                    """,
                    (f"job-{suffix}", run_id, started),
                )
                connection.execute(
                    """
                    INSERT INTO watch_folder_observations
                        (watch_id, folder_name, config_revision,
                         folder_device, folder_inode, inventory_id,
                         inventory_payload, snapshot_id, snapshot_payload,
                         first_observed_at, stable_at, discovery_id, status,
                         retry_count)
                    VALUES (
                        %s, 'retry-folder', %s, 1, 2, %s,
                        '{}'::jsonb, %s, '{}'::jsonb,
                        %s, %s, %s, 'active', 2
                    )
                    """,
                    (
                        watch_id,
                        config.revision,
                        f"inventory-{suffix}",
                        f"snapshot-{suffix}",
                        started,
                        started,
                        discovery_id,
                    ),
                )

        scheduler = PostgresSchedulerRepository(control.pool)
        assert scheduler.retry_folder_generation(
            run_id=run_id,
            max_retries=3,
        ) == 3
        with control.pool.connection() as connection:
            observation = connection.execute(
                """
                SELECT discovery_id, status, retry_count
                FROM watch_folder_observations
                WHERE watch_id = %s AND folder_name = 'retry-folder'
                """,
                (watch_id,),
            ).fetchone()
            run_status = connection.execute(
                "SELECT status FROM runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            job_status = connection.execute(
                "SELECT status FROM jobs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        assert observation == (None, "settling", 3)
        assert run_status == ("failed",)
        assert job_status == ("failed",)
    finally:
        control.close()


@pytest.mark.postgres
def test_folder_run_deletion_allows_detached_or_missing_observation() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-folder-delete-{suffix}"
    detached_discovery = f"discovery-detached-{suffix}"
    active_discovery = f"discovery-active-{suffix}"
    detached_run = f"run-detached-{suffix}"
    active_run = f"run-active-{suffix}"
    started = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        config = _ensure_config(control, suffix)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                for discovery_id, folder_name, generation in (
                    (
                        detached_discovery,
                        "detached-folder",
                        f"generation-detached-{suffix}",
                    ),
                    (
                        active_discovery,
                        "active-folder",
                        f"generation-active-{suffix}",
                    ),
                ):
                    connection.execute(
                        """
                        INSERT INTO discoveries
                            (discovery_id, watch_id, config_revision,
                             snapshot_id, snapshot_payload, work_type,
                             discovered_at, source_folder,
                             folder_generation_id, inventory_id)
                        VALUES (
                            %s, %s, %s, %s,
                            '{"files":[],"snapshot_id":"empty"}'::jsonb,
                            'anime', %s, %s, %s, %s
                        )
                        """,
                        (
                            discovery_id,
                            watch_id,
                            config.revision,
                            f"snapshot-{generation}",
                            started,
                            folder_name,
                            generation,
                            f"inventory-{generation}",
                        ),
                    )
                for run_id, discovery_id in (
                    (detached_run, detached_discovery),
                    (active_run, active_discovery),
                ):
                    connection.execute(
                        """
                        INSERT INTO runs
                            (run_id, discovery_id, config_revision,
                             work_type, source_capability, status)
                        VALUES (%s, %s, %s, 'anime', %s, 'failed')
                        """,
                        (
                            run_id,
                            discovery_id,
                            config.revision,
                            f"source-{run_id}",
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO jobs
                            (job_id, run_id, status, updated_at)
                        VALUES (%s, %s, 'completed', %s)
                        """,
                        (f"job-{run_id}", run_id, started),
                    )
                connection.execute(
                    """
                    INSERT INTO watch_folder_observations
                        (watch_id, folder_name, config_revision,
                         folder_device, folder_inode, inventory_id,
                         inventory_payload, snapshot_id, snapshot_payload,
                         first_observed_at, stable_at, discovery_id, status)
                    VALUES (
                        %s, 'active-folder', %s, 1, 2, %s,
                        '{}'::jsonb, %s, '{}'::jsonb,
                        %s, %s, %s, 'active'
                    )
                    """,
                    (
                        watch_id,
                        config.revision,
                        f"inventory-active-{suffix}",
                        f"snapshot-active-{suffix}",
                        started,
                        started,
                        active_discovery,
                    ),
                )

        queries = PostgresQueries(control.pool)
        deletion = PostgresRunDeletionService(control.pool)
        detached = queries.get_run(detached_run)
        active = queries.get_run(active_run)
        assert detached is not None
        assert active is not None
        assert "delete_run" in detached["available_actions"]
        assert "delete_run" not in active["available_actions"]
        summaries = {
            item["run_id"]: item
            for item in queries.list_runs(before=None, limit=10_000)
        }
        assert summaries[detached_run]["available_actions"] == [
            "delete_run"
        ]
        assert summaries[active_run]["available_actions"] == []

        deletion.delete(detached_run)
        with pytest.raises(ServerError) as error:
            deletion.delete(active_run)
        assert error.value.code is ServerErrorCode.RUN_DELETE_CONFLICT

        scheduler = PostgresSchedulerRepository(control.pool)
        first = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started + timedelta(seconds=1),
            scan=FolderScan(()),
        )
        second = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started + timedelta(seconds=2),
            scan=FolderScan(()),
        )
        assert first.mutated
        assert second.mutated
        with control.pool.connection() as connection:
            missing_status = connection.execute(
                """
                SELECT status, blocked_reason
                FROM watch_folder_observations
                WHERE discovery_id = %s
                """,
                (active_discovery,),
            ).fetchone()
        assert missing_status == (
            "blocked",
            "source_folder_missing",
        )
        missing = queries.get_run(active_run)
        assert missing is not None
        assert "delete_run" in missing["available_actions"]
        deletion.delete(active_run)
        with control.pool.connection() as connection:
            remaining = connection.execute(
                """
                SELECT 1 FROM watch_folder_observations
                WHERE discovery_id = %s
                """,
                (active_discovery,),
            ).fetchone()
        assert remaining is None
    finally:
        control.close()
