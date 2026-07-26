from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.queries import PostgresQueries


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_run_and_discovery_pages_order_by_creation_time() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-order-{suffix}"
    discovery_ids = (
        f"discovery-z-old-{suffix}",
        f"discovery-m-middle-{suffix}",
        f"discovery-a-new-{suffix}",
    )
    run_ids = (
        f"run-z-old-{suffix}",
        f"run-m-middle-{suffix}",
        f"run-a-new-{suffix}",
    )
    moments = (
        datetime(2099, 1, 1, tzinfo=UTC),
        datetime(2099, 1, 2, tzinfo=UTC),
        datetime(2099, 1, 3, tzinfo=UTC),
    )
    try:
        control.open()
        control.migrate()
        config = PostgresConfigRepository(control.pool).head()
        assert config is not None
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
                for index, (discovery_id, run_id, moment) in enumerate(
                    zip(discovery_ids, run_ids, moments, strict=True)
                ):
                    connection.execute(
                        """
                        INSERT INTO discoveries
                            (discovery_id, watch_id, config_revision,
                             snapshot_id, snapshot_payload, work_type,
                             discovered_at)
                        VALUES (
                            %s, %s, %s, %s,
                            '{"files":[],"snapshot_id":"empty"}'::jsonb,
                            'anime', %s
                        )
                        """,
                        (
                            discovery_id,
                            watch_id,
                            config.revision,
                            f"snapshot-{index}-{suffix}",
                            moment,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO runs
                            (run_id, discovery_id, config_revision,
                             work_type, source_capability, status,
                             created_at)
                        VALUES (%s, %s, %s, 'anime', %s, 'registered', %s)
                        """,
                        (
                            run_id,
                            discovery_id,
                            config.revision,
                            f"source-{index}-{suffix}",
                            moment,
                        ),
                    )
        queries = PostgresQueries(control.pool)

        runs = queries.list_runs(before=None, limit=10_000)
        discoveries = queries.list_discoveries(before=None, limit=10_000)

        run_positions = {
            item["run_id"]: index for index, item in enumerate(runs)
        }
        discovery_positions = {
            item["discovery_id"]: index
            for index, item in enumerate(discoveries)
        }
        assert run_positions[run_ids[2]] < run_positions[run_ids[1]]
        assert run_positions[run_ids[1]] < run_positions[run_ids[0]]
        assert (
            discovery_positions[discovery_ids[2]]
            < discovery_positions[discovery_ids[1]]
            < discovery_positions[discovery_ids[0]]
        )
        older_runs = queries.list_runs(
            before=run_ids[1],
            limit=10_000,
        )
        older_discoveries = queries.list_discoveries(
            before=discovery_ids[1],
            limit=10_000,
        )
        assert run_ids[0] in {item["run_id"] for item in older_runs}
        assert run_ids[2] not in {item["run_id"] for item in older_runs}
        assert discovery_ids[0] in {
            item["discovery_id"] for item in older_discoveries
        }
        assert discovery_ids[2] not in {
            item["discovery_id"] for item in older_discoveries
        }
    finally:
        control.close()
