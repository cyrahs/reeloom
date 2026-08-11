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
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import RunStarted, RunStopped, StopReason
from reeloom.server.runtime_store import PostgresEventStore


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
        assert discovery_id not in {
            item["discovery_id"]
            for item in queries.list_discoveries(
                before=None, limit=10_000
            )
        }

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
def test_forward_agent_failure_has_canonical_delete_escape() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-failed-delete-{suffix}"
    discovery_id = f"discovery-failed-delete-{suffix}"
    run_id = f"run-failed-delete-{suffix}"
    inventory_id = f"folder-inventory-v2:{uuid.uuid4().hex * 2}"
    snapshot_id = f"candidate-snapshot-v2:{uuid.uuid4().hex * 2}"
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
                         settle_interval_seconds, semantic_v2)
                    VALUES (%s, %s, %s, 'anime', 1, true)
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
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime',
                            clock_timestamp(), 'FailedFolder', %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        snapshot_id,
                        f"folder-generation-v2:{uuid.uuid4().hex * 2}",
                        inventory_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'registered')
                    """,
                    (run_id, discovery_id, config.revision, f"source-{suffix}"),
                )
                connection.execute(
                    """
                    INSERT INTO jobs (job_id, run_id, status)
                    VALUES (%s, %s, 'running')
                    """,
                    (f"job-{suffix}", run_id),
                )
                connection.execute(
                    """
                    INSERT INTO watch_folder_observations
                        (watch_id, folder_name, config_revision,
                         folder_device, folder_inode, inventory_id,
                         inventory_payload, snapshot_id, snapshot_payload,
                         first_observed_at, stable_at, discovery_id, status)
                    VALUES (%s, 'FailedFolder', %s, NULL, NULL, %s,
                            '{}'::jsonb, %s, '{}'::jsonb,
                            clock_timestamp(), clock_timestamp(), %s, 'active')
                    """,
                    (
                        watch_id,
                        config.revision,
                        inventory_id,
                        snapshot_id,
                        discovery_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason)
                    VALUES (%s, 'forward_v2', 'test_failure')
                    """,
                    (run_id,),
                )
        PostgresEventStore(control.pool, run_id=run_id).append(
            RunStarted(run_id, TmdbWorkType.ANIME, RunBudget())
        )

        PostgresSchedulerRepository(control.pool).terminalize_run_failure(
            run_id=run_id,
            failure_code="internal_error",
        )

        queries = PostgresQueries(control.pool)
        run = queries.get_run(run_id)
        assert run is not None
        assert run["status"] == "failed"
        assert run["available_actions"] == ["delete_run"]
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT outcome, reason_code, source_disposition
                FROM planning_terminal_results_v2
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone() == ("agent_failed", "internal_error", "preserve")

        PostgresRunDeletionService(control.pool).delete(run_id)
        assert queries.get_run(run_id) is None
    finally:
        control.close()


@pytest.mark.postgres
def test_retry_agent_terminalizes_old_generation_before_fresh_scan() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-retry-agent-{suffix}"
    discovery_id = f"discovery-retry-agent-{suffix}"
    run_id = f"run-retry-agent-{suffix}"
    inventory_id = f"folder-inventory-v2:{uuid.uuid4().hex * 2}"
    snapshot_id = f"candidate-snapshot-v2:{uuid.uuid4().hex * 2}"
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
                         settle_interval_seconds, semantic_v2)
                    VALUES (%s, %s, %s, 'anime', 1, true)
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
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime',
                            clock_timestamp(), 'RetryAgent', %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        snapshot_id,
                        f"folder-generation-v2:{uuid.uuid4().hex * 2}",
                        inventory_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'registered')
                    """,
                    (run_id, discovery_id, config.revision, f"source-{suffix}"),
                )
                connection.execute(
                    """
                    INSERT INTO jobs (job_id, run_id, status)
                    VALUES (%s, %s, 'completed')
                    """,
                    (f"job-{suffix}", run_id),
                )
                connection.execute(
                    """
                    INSERT INTO watch_folder_observations
                        (watch_id, folder_name, config_revision,
                         inventory_id, inventory_payload, snapshot_id,
                         snapshot_payload, first_observed_at, stable_at,
                         discovery_id, status, retry_count)
                    VALUES (%s, 'RetryAgent', %s, %s, '{}'::jsonb, %s,
                            '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                            %s, 'active', 0)
                    """,
                    (
                        watch_id,
                        config.revision,
                        inventory_id,
                        snapshot_id,
                        discovery_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason)
                    VALUES (%s, 'forward_v2', 'test_retry_agent')
                    """,
                    (run_id,),
                )
        store = PostgresEventStore(control.pool, run_id=run_id)
        store.append(RunStarted(run_id, TmdbWorkType.ANIME, RunBudget()))
        store.append(RunStopped(StopReason.NEEDS_ATTENTION))
        scheduler = PostgresSchedulerRepository(control.pool)

        assert scheduler.retry_needs_attention(
            run_id=run_id,
            expected_event_sequence=2,
        ) == 1

        queries = PostgresQueries(control.pool)
        detail = queries.get_run(run_id)
        assert detail is not None
        assert detail["status"] == "failed"
        assert detail["available_actions"] == ["delete_run"]
        summary = next(
            item
            for item in queries.list_runs(before=None, limit=10_000)
            if item["run_id"] == run_id
        )
        assert summary["status"] == "failed"
        assert summary["available_actions"] == ["delete_run"]
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT outcome, reason_code
                FROM planning_terminal_results_v2
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone() == ("agent_failed", "generation_invalidated")
            assert connection.execute(
                """
                SELECT discovery_id, status, retry_count
                FROM watch_folder_observations
                WHERE watch_id = %s AND folder_name = 'RetryAgent'
                """,
                (watch_id,),
            ).fetchone() == (None, "settling", 1)
        PostgresRunDeletionService(control.pool).delete(run_id)
        assert queries.get_run(run_id) is None
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
        assert "delete_run" in active["available_actions"]
        summaries = {
            item["run_id"]: item
            for item in queries.list_runs(before=None, limit=10_000)
        }
        assert summaries[detached_run]["available_actions"] == [
            "delete_run"
        ]
        assert summaries[active_run]["available_actions"] == [
            "delete_run"
        ]

        deletion.delete(detached_run)

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
