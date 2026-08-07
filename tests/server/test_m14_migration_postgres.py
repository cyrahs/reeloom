from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql

from reeloom.server.migrations import MIGRATIONS


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_m14_supersedes_unsettled_v1_without_filesystem_effect() -> None:
    schema = "m14_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS[:36]:
            connection.execute(migration.sql)
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES ('config:1', 1, '{}'::jsonb, clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds)
            VALUES ('watch:1', 1, 1, 'anime', 1);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES
                ('discovery:1', 'watch:1', 1, 'snapshot:v1', '{}'::jsonb,
                 'anime', clock_timestamp(), 'Incoming', 'generation:v1',
                 'folder-inventory-v1:' || repeat('a', 64)),
                ('discovery:2', 'watch:1', 1, 'snapshot:unclaimed-v1',
                 '{}'::jsonb, 'anime', clock_timestamp(), 'Waiting',
                 'generation:unclaimed-v1',
                 'folder-inventory-v1:' || repeat('c', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES
                ('run:1', 'discovery:1', 1, 'anime', 'capability:1',
                 'applying'),
                ('run:2', 'discovery:2', 1, 'anime', 'capability:2',
                 'awaiting_approval');
            INSERT INTO jobs (job_id, run_id, status)
            VALUES
                ('job:1', 'run:1', 'running'),
                ('job:2', 'run:2', 'completed');
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision, folder_device,
                 folder_inode, inventory_id, inventory_payload, snapshot_id,
                 snapshot_payload, first_observed_at, stable_at,
                 discovery_id, status)
            VALUES
                ('watch:1', 'Incoming', 1, 1, 2,
                 'folder-inventory-v1:' || repeat('a', 64), '{}'::jsonb,
                 'snapshot:v1', '{}'::jsonb, clock_timestamp(),
                clock_timestamp(), 'discovery:1', 'active');
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision, folder_device,
                 folder_inode, inventory_id, inventory_payload, snapshot_id,
                 snapshot_payload, first_observed_at, stable_at,
                 discovery_id, status)
            VALUES
                ('watch:1', 'Waiting', 1, 3, 4,
                 'folder-inventory-v1:' || repeat('c', 64), '{}'::jsonb,
                 'snapshot:unclaimed-v1', '{}'::jsonb, clock_timestamp(),
                 clock_timestamp(), 'discovery:2', 'active');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:1', 1, 'sha256:' || repeat('b', 64), 'initial');
            INSERT INTO approvals
                (approval_id, run_id, plan_hash, scope, expires_at,
                 canonical_record)
            VALUES
                ('approval:1', 'run:1', 'sha256:' || repeat('b', 64),
                 'apply', clock_timestamp() + interval '1 hour', '\\x00');
            INSERT INTO approval_claims (approval_id, run_id, plan_hash)
            VALUES
                ('approval:1', 'run:1', 'sha256:' || repeat('b', 64));
            INSERT INTO run_operations
                (run_id, operation_id, operation_kind)
            VALUES ('run:1', 'operation:1', 'recover');
            """
        )

        connection.execute(MIGRATIONS[36].sql)

        run = connection.execute(
            "SELECT status FROM runs WHERE run_id = 'run:1'"
        ).fetchone()
        job = connection.execute(
            "SELECT status, boot_id FROM jobs WHERE run_id = 'run:1'"
        ).fetchone()
        observation = connection.execute(
            """
            SELECT discovery_id, status
            FROM watch_folder_observations
            WHERE watch_id = 'watch:1' AND folder_name = 'Incoming'
            """
        ).fetchone()
        legacy = connection.execute(
            """
            SELECT media_unsettled, folder_unsettled,
                   subtitle_unsettled, fresh_scan_dispatched
            FROM legacy_effect_supersessions_v2
            WHERE run_id = 'run:1'
            """
        ).fetchone()

        assert run == ("superseded",)
        assert job == ("completed", None)
        assert observation == (None, "settling")
        assert legacy == (True, False, False, True)
        assert connection.execute(
            "SELECT 1 FROM run_operations WHERE run_id = 'run:1'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM approval_settlements WHERE approval_id = 'approval:1'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT status FROM runs WHERE run_id = 'run:2'"
        ).fetchone() == ("superseded",)
        assert connection.execute(
            """
            SELECT discovery_id, status
            FROM watch_folder_observations
            WHERE watch_id = 'watch:1' AND folder_name = 'Waiting'
            """
        ).fetchone() == (None, "settling")
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()
