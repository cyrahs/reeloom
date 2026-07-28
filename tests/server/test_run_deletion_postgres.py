from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

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


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


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
        repository = PostgresConfigRepository(control.pool)
        config = repository.head()
        if config is None:
            config = repository.compare_and_append(
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
                         snapshot_id, snapshot_payload, work_type)
                    VALUES (
                        %s, %s, %s, %s,
                        '{"files":[],"snapshot_id":"empty"}'::jsonb,
                        'anime'
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
