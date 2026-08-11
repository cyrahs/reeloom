from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import CheckViolation

from reeloom.server.migrations import MIGRATIONS
from reeloom.server.run_deletion_policy import RUN_DELETION_READY_SQL


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


@pytest.mark.postgres
def test_m14_semantic_observation_omits_only_v2_stat_identity() -> None:
    schema = "m14_identity_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS:
            connection.execute(migration.sql)
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES ('config:identity', 1, '{}'::jsonb, clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds, semantic_v2)
            VALUES ('watch:identity', 1, 1, 'anime', 1, true);
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision,
                 folder_device, folder_inode, inventory_id,
                 inventory_payload, snapshot_id, snapshot_payload,
                 first_observed_at, status)
            VALUES
                ('watch:identity', 'Semantic', 1, NULL, NULL,
                 'folder-inventory-v2:' || repeat('a', 64), '{}'::jsonb,
                 'candidate-snapshot-v2:' || repeat('b', 64), '{}'::jsonb,
                 clock_timestamp(), 'settling');
            """
        )

        with pytest.raises(CheckViolation):
            connection.execute(
                """
                INSERT INTO watch_folder_observations
                    (watch_id, folder_name, config_revision,
                     folder_device, folder_inode, inventory_id,
                     inventory_payload, snapshot_id, snapshot_payload,
                     first_observed_at, status)
                VALUES
                    ('watch:identity', 'Legacy', 1, NULL, NULL,
                     'folder-inventory-v1:' || repeat('c', 64),
                     '{}'::jsonb, 'candidate-snapshot-v1:' || repeat('d', 64),
                     '{}'::jsonb, clock_timestamp(), 'settling')
                """
            )
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()


@pytest.mark.postgres
def test_m14_6_quarantines_published_legacy_subtitle_handoff() -> None:
    schema = "m14_6_handoff_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS[:40]:
            connection.execute(migration.sql)
        lineage = "subtitle-lineage-v1-" + "1" * 64
        plan_hash = "sha256:" + "2" * 64
        publication_id = "subtitle-publication-v2-" + "3" * 64
        request_id = "subtitle-scan-v2-" + "4" * 64
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES ('config:m14-6', 1,
                    '{"apply_policy":"manual"}'::jsonb,
                    clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds, semantic_v2)
            VALUES ('watch:m14-6', 1, 1, 'anime', 1, true);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES
                ('discovery:m14-6', 'watch:m14-6', 1,
                 'candidate-snapshot-v2:' || repeat('5', 64),
                 '{}'::jsonb, 'anime', clock_timestamp(),
                 'LegacyPublished', 'folder-generation-v2:' || repeat('6', 64),
                 'folder-inventory-v2:' || repeat('7', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:m14-6', 'discovery:m14-6', 1, 'anime',
                    'capability:m14-6', 'completed');
            """
        )
        connection.execute(
            """
            INSERT INTO subtitle_acquisition_lineages
                (lineage_key, root_discovery_id)
            VALUES (%s, 'discovery:m14-6');
            """,
            (lineage,),
        )
        connection.execute(
            """
            INSERT INTO subtitle_publication_settlements_v2
                (lineage_key, origin_run_id, acquisition_plan_hash,
                 approval_id, publication_id, watch_id, source_folder,
                 publication_directory, manifest_digest, member_count)
            VALUES (%s, 'run:m14-6', %s, 'approval:m14-6', %s,
                    'watch:m14-6', 'LegacyPublished',
                    'reeloom-acquired-' || repeat('8', 64),
                    repeat('9', 64), 1);
            """,
            (lineage, plan_hash, publication_id),
        )
        connection.execute(
            """
            INSERT INTO subtitle_scan_requests_v2
                (request_id, lineage_key, run_id, watch_id, source_folder)
            VALUES (%s, %s, 'run:m14-6', 'watch:m14-6',
                    'LegacyPublished');
            """,
            (request_id, lineage),
        )
        connection.execute(
            """
            INSERT INTO notification_outbox
                (notification_id, dedupe_key, notification_type,
                 schema_version, payload_json)
            VALUES ('notification:m14-6', %s, 'plan_ready', 1,
                    '{"legacy":true}'::jsonb)
            """,
            ("plan_ready:" + plan_hash,),
        )
        legacy_media_plan_hash = "sha256:" + "a" * 64
        connection.execute(
            """
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES
                ('discovery:legacy-operation', 'watch:m14-6', 1,
                 'candidate-snapshot-v2:' || repeat('b', 64),
                 '{}'::jsonb, 'anime', clock_timestamp(),
                 'LegacyOperation',
                 'folder-generation-v2:' || repeat('c', 64),
                 'folder-inventory-v2:' || repeat('d', 64))
            """
        )
        connection.execute(
            """
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES
                ('run:legacy-operation', 'discovery:legacy-operation', 1,
                 'anime', 'capability:legacy-operation', 'applying')
            """
        )
        connection.execute(
            """
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:legacy-operation', 1, %s, 'initial')
            """,
            (legacy_media_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO approvals
                (approval_id, run_id, plan_hash, scope, expires_at,
                 canonical_record)
            VALUES
                ('approval:legacy-operation', 'run:legacy-operation', %s,
                 'apply', clock_timestamp() + interval '1 hour', '\\x00')
            """,
            (legacy_media_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO execution_operations_v2
                (operation_id, schema_version, run_id, plan_hash,
                 approval_id, operation_kind, status)
            VALUES
                ('operation:legacy-operation', 2, 'run:legacy-operation',
                 %s, 'approval:legacy-operation', 'media_move',
                 'authorized')
            """,
            (legacy_media_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO legacy_effect_supersessions_v2
                (run_id, discovery_id, watch_id, source_folder,
                 media_unsettled, folder_unsettled, subtitle_unsettled)
            VALUES
                ('run:legacy-operation', 'discovery:legacy-operation',
                 'watch:m14-6', 'LegacyOperation', true, false, false)
            """
        )
        connection.execute(
            """
            INSERT INTO notification_outbox
                (notification_id, dedupe_key, notification_type,
                 schema_version, payload_json)
            VALUES
                ('notification:legacy-media', 'plan_ready:' || %s,
                 'plan_ready', 1, '{"legacy_media":true}'::jsonb)
            """,
            (legacy_media_plan_hash,),
        )
        forward_rescan_plan_hash = "sha256:" + "4" * 64
        connection.execute(
            """
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES
                ('discovery:forward-rescan', 'watch:m14-6', 1,
                 'candidate-snapshot-v2:' || repeat('5', 64),
                 '{}'::jsonb, 'anime', clock_timestamp(),
                 'ForwardRescan',
                 'folder-generation-v2:' || repeat('e', 64),
                 'folder-inventory-v2:' || repeat('f', 64))
            """
        )
        connection.execute(
            """
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES
                ('run:forward-rescan', 'discovery:forward-rescan', 1,
                 'anime', 'capability:forward-rescan', 'failed')
            """
        )
        connection.execute(
            """
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:forward-rescan', 1, %s, 'initial')
            """,
            (forward_rescan_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO approvals
                (approval_id, run_id, plan_hash, scope, expires_at,
                 canonical_record)
            VALUES
                ('approval:forward-rescan', 'run:forward-rescan', %s,
                 'apply', clock_timestamp() + interval '1 hour', '\\x00')
            """,
            (forward_rescan_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO execution_operations_v2
                (operation_id, schema_version, run_id, plan_hash,
                 approval_id, operation_kind, status, attempt_count,
                 outcomes)
            VALUES
                ('operation:forward-rescan', 2, 'run:forward-rescan', %s,
                 'approval:forward-rescan', 'media_move', 'collision', 1,
                 '["collision"]'::jsonb)
            """,
            (forward_rescan_plan_hash,),
        )
        connection.execute(
            """
            INSERT INTO execution_operation_results_v2
                (operation_id, items, fresh_scan_required)
            VALUES
                ('operation:forward-rescan',
                 '[{"source_id":"video:1","outcome":"collision"}]'::jsonb,
                 true)
            """
        )
        connection.execute(
            """
            INSERT INTO execution_rescan_outbox_v2
                (operation_id, run_id, state, attempt_count)
            VALUES
                ('operation:forward-rescan', 'run:forward-rescan',
                 'retry_wait', 9)
            """
        )

        connection.execute(MIGRATIONS[40].sql)

        assert connection.execute(
            """
            SELECT mode, classification_reason, effect_plan_hash,
                   operation_id
            FROM run_lifecycle_controls_v2
            WHERE run_id = 'run:m14-6'
            """
        ).fetchone() == (
            "legacy_read_only",
            "legacy_subtitle_history",
            None,
            None,
        )
        handoff = connection.execute(
            """
            SELECT request_kind, origin_run_id, watch_id, source_folder,
                   lineage_key, state
            FROM generation_requests_v2
            WHERE origin_run_id = 'run:m14-6'
            """
        ).fetchone()
        assert handoff == (
            "legacy_handoff",
            "run:m14-6",
            "watch:m14-6",
            "LegacyPublished",
            lineage,
            "queued",
        )
        assert connection.execute(
            """
            SELECT state FROM subtitle_scan_requests_v2
            WHERE request_id = %s
            """,
            (request_id,),
        ).fetchone() == ("queued",)
        assert connection.execute(
            "SELECT count(*) FROM execution_operations_v2"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM approvals"
        ).fetchone() == (2,)
        assert connection.execute(
            """
            SELECT control.mode, control.operation_id, operation.status
            FROM run_lifecycle_controls_v2 AS control
            JOIN execution_operations_v2 AS operation
              ON operation.run_id = control.run_id
            WHERE control.run_id = 'run:legacy-operation'
            """
        ).fetchone() == ("legacy_read_only", None, "superseded")
        assert connection.execute(
            """
            SELECT request.request_kind, request.state,
                   request.attempt_count, request.operation_id
            FROM generation_requests_v2 AS request
            WHERE request.operation_id = 'operation:forward-rescan'
            """
        ).fetchone() == (
            "operation_rescan",
            "queued",
            0,
            "operation:forward-rescan",
        )
        assert connection.execute(
            """
            SELECT state, lease_owner, lease_expires_at
            FROM notification_outbox
            WHERE notification_id = 'notification:m14-6'
            """
        ).fetchone() == ("cancelled", None, None)
        assert connection.execute(
            """
            SELECT state FROM notification_outbox
            WHERE notification_id = 'notification:legacy-media'
            """
        ).fetchone() == ("cancelled",)

        # Simulate a row created after 0041 on a folder already owned by the
        # active legacy handoff.  0042 must retain a blocked diagnostic instead
        # of silently losing the operation rescan, and must cancel its stale
        # approval notification.
        connection.execute(
            """
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES ('discovery:rescan-conflict', 'watch:m14-6', 1,
                    'candidate-snapshot-v2:' || repeat('7', 64),
                    '{}'::jsonb, 'anime', clock_timestamp(),
                    'LegacyPublished',
                    'folder-generation-v2:' || repeat('8', 64),
                    'folder-inventory-v2:' || repeat('9', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:rescan-conflict', 'discovery:rescan-conflict', 1,
                    'anime', 'capability:rescan-conflict', 'failed');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:rescan-conflict', 1,
                    'sha256:' || repeat('6', 64), 'initial');
            INSERT INTO approvals
                (approval_id, run_id, plan_hash, scope, expires_at,
                 canonical_record)
            VALUES ('approval:rescan-conflict', 'run:rescan-conflict',
                    'sha256:' || repeat('6', 64), 'apply',
                    clock_timestamp() + interval '1 hour', '\\x00');
            INSERT INTO execution_operations_v2
                (operation_id, schema_version, run_id, plan_hash,
                 approval_id, operation_kind, status, attempt_count, outcomes)
            VALUES ('operation:rescan-conflict', 2, 'run:rescan-conflict',
                    'sha256:' || repeat('6', 64),
                    'approval:rescan-conflict', 'media_move', 'collision', 1,
                    '["collision"]'::jsonb);
            INSERT INTO execution_operation_results_v2
                (operation_id, items, fresh_scan_required)
            VALUES ('operation:rescan-conflict',
                    '[{"source_id":"video:1","outcome":"collision"}]'::jsonb,
                    true);
            INSERT INTO run_lifecycle_controls_v2
                (run_id, mode, classification_reason, revision, effect_kind,
                 effect_plan_hash, effect_policy, operation_id,
                 handoff_event_sequence)
            VALUES ('run:rescan-conflict', 'forward_v2', 'test_conflict', 1,
                    'media_move', 'sha256:' || repeat('6', 64), 'manual',
                    'operation:rescan-conflict', 1);
            INSERT INTO execution_rescan_outbox_v2
                (operation_id, run_id, state)
            VALUES ('operation:rescan-conflict', 'run:rescan-conflict',
                    'queued');
            INSERT INTO notification_outbox
                (notification_id, dedupe_key, notification_type,
                 schema_version, payload_json)
            VALUES ('notification:rescan-conflict',
                    'plan_ready:sha256:' || repeat('6', 64), 'plan_ready', 1,
                    '{"stale":true}'::jsonb);
            """
        )
        connection.execute(MIGRATIONS[41].sql)
        assert connection.execute(
            """
            SELECT state, warning
            FROM generation_requests_v2
            WHERE operation_id = 'operation:rescan-conflict'
            """
        ).fetchone() == ("blocked", "legacy_generation_conflict")
        assert connection.execute(
            """
            SELECT state FROM notification_outbox
            WHERE notification_id = 'notification:rescan-conflict'
            """
        ).fetchone() == ("cancelled",)
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()


@pytest.mark.postgres
def test_m14_6_terminalizes_existing_semantic_plan_only_run() -> None:
    schema = "m14_6_plan_only_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS[:40]:
            connection.execute(migration.sql)
        plan_hash = "sha256:" + "a" * 64
        inventory_id = "folder-inventory-v2:" + "b" * 64
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES ('config:plan-only', 1,
                    '{"apply_policy":"plan_only"}'::jsonb,
                    clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds, semantic_v2)
            VALUES ('watch:plan-only', 1, 1, 'anime', 1, true);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES ('discovery:plan-only', 'watch:plan-only', 1,
                    'candidate-snapshot-v2:' || repeat('c', 64),
                    '{}'::jsonb, 'anime', clock_timestamp(), 'PlanOnly',
                        'folder-generation-v2:' || repeat('d', 64),
                        'folder-inventory-v2:' || repeat('b', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:plan-only', 'discovery:plan-only', 1, 'anime',
                    'capability:plan-only', 'awaiting_approval');
            INSERT INTO jobs (job_id, run_id, status)
            VALUES ('job:plan-only', 'run:plan-only', 'completed');
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision,
                 folder_device, folder_inode, inventory_id,
                 inventory_payload, snapshot_id, snapshot_payload,
                 first_observed_at, stable_at, discovery_id, status)
            VALUES ('watch:plan-only', 'PlanOnly', 1, NULL, NULL,
                    'folder-inventory-v2:' || repeat('b', 64),
                    '{}'::jsonb,
                    'candidate-snapshot-v2:' || repeat('c', 64),
                    '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                    'discovery:plan-only', 'active');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:plan-only', 1,
                    'sha256:' || repeat('a', 64), 'initial');
            INSERT INTO plan_heads (run_id, version, plan_hash)
            VALUES ('run:plan-only', 1,
                    'sha256:' || repeat('a', 64));
            INSERT INTO run_states
                (run_id, event_sequence, phase, runtime_status,
                 model_turns, model_tokens, tool_calls, failures,
                 plan_hash, deadline_at, projection_schema,
                 projection_payload)
            VALUES ('run:plan-only', 1, 'awaiting_approval', 'stopped',
                    0, 0, 0, 0, 'sha256:' || repeat('a', 64),
                    clock_timestamp() + interval '1 hour',
                    'test-v1', '{}'::jsonb);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES ('discovery:failed', 'watch:plan-only', 1,
                    'candidate-snapshot-v2:' || repeat('e', 64),
                    '{}'::jsonb, 'anime', clock_timestamp(), 'FailedRun',
                    'folder-generation-v2:' || repeat('f', 64),
                    'folder-inventory-v2:' || repeat('1', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:failed', 'discovery:failed', 1, 'anime',
                    'capability:failed', 'failed');
            INSERT INTO jobs (job_id, run_id, status)
            VALUES ('job:failed', 'run:failed', 'failed');
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision,
                 folder_device, folder_inode, inventory_id,
                 inventory_payload, snapshot_id, snapshot_payload,
                 first_observed_at, stable_at, discovery_id, status)
            VALUES ('watch:plan-only', 'FailedRun', 1, NULL, NULL,
                    'folder-inventory-v2:' || repeat('1', 64),
                    '{}'::jsonb,
                    'candidate-snapshot-v2:' || repeat('e', 64),
                    '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                    'discovery:failed', 'active');
            """
        )

        connection.execute(MIGRATIONS[40].sql)

        assert connection.execute(
            """
            SELECT mode, effect_kind, effect_plan_hash, effect_policy,
                   operation_id
            FROM run_lifecycle_controls_v2
            WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == (
            "forward_v2",
            "media_move",
            plan_hash,
            "plan_only",
            None,
        )
        assert connection.execute(
            """
            SELECT outcome, reason_code, source_disposition
            FROM planning_terminal_results_v2
            WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == (
            "plan_only",
            "plan_only_migration",
            "preserve",
        )
        assert connection.execute(
            """
            SELECT terminal_status, inventory_id
            FROM handled_folder_inventories_v2
            WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == ("completed", inventory_id)
        assert connection.execute(
            "SELECT status FROM runs WHERE run_id = 'run:plan-only'"
        ).fetchone() == ("completed",)
        assert connection.execute(
            """
            SELECT phase, runtime_status,
                   projection_payload->>'phase',
                   projection_payload->>'status'
            FROM run_states WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == ("completed", "stopped", "completed", "stopped")
        assert connection.execute(
            """
            SELECT count(*) FROM execution_operations_v2
            WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT intent_kind, state FROM notification_intents_v2
            WHERE run_id = 'run:plan-only'
            """
        ).fetchone() == ("plan_generated", "queued")
        assert connection.execute(
            """
            SELECT terminal.outcome, terminal.reason_code,
                   terminal.source_disposition, handled.terminal_status
            FROM planning_terminal_results_v2 AS terminal
            JOIN handled_folder_inventories_v2 AS handled
              ON handled.run_id = terminal.run_id
            WHERE terminal.run_id = 'run:failed'
            """
        ).fetchone() == (
            "agent_failed",
            "pre_m14_6_terminal_failure",
            "preserve",
            "agent_failed",
        )
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()


@pytest.mark.postgres
def test_m14_6_quarantines_completed_v1_media_history() -> None:
    schema = "m14_6_v1_history_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS[:40]:
            connection.execute(migration.sql)
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES
                ('config:v1-manual', 1,
                 '{"apply_policy":"manual"}'::jsonb, clock_timestamp()),
                ('config:v1-auto', 2,
                 '{"apply_policy":"automatic"}'::jsonb,
                 clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds, semantic_v2)
            VALUES
                ('watch:v1-manual', 1, 1, 'anime', 1, false),
                ('watch:v1-auto', 2, 2, 'anime', 1, false);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES
                ('discovery:v1-manual', 'watch:v1-manual', 1,
                 'candidate-snapshot-v1:' || repeat('1', 64),
                 '{}'::jsonb, 'anime', clock_timestamp(), 'Manual',
                 'folder:v1-manual',
                 'folder-inventory-v1:' || repeat('1', 64)),
                ('discovery:v1-auto', 'watch:v1-auto', 2,
                 'candidate-snapshot-v1:' || repeat('2', 64),
                 '{}'::jsonb, 'anime', clock_timestamp(), 'Automatic',
                 'folder:v1-auto',
                 'folder-inventory-v1:' || repeat('2', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES
                ('run:v1-manual', 'discovery:v1-manual', 1, 'anime',
                 'capability:v1-manual', 'completed'),
                ('run:v1-auto', 'discovery:v1-auto', 2, 'anime',
                 'capability:v1-auto', 'completed');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES
                ('run:v1-manual', 1,
                 'sha256:' || repeat('a', 64), 'initial'),
                ('run:v1-auto', 1,
                 'sha256:' || repeat('b', 64), 'initial');
            INSERT INTO plan_heads (run_id, version, plan_hash)
            VALUES
                ('run:v1-manual', 1, 'sha256:' || repeat('a', 64)),
                ('run:v1-auto', 1, 'sha256:' || repeat('b', 64));
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision, folder_device,
                 folder_inode, inventory_id, inventory_payload, snapshot_id,
                 snapshot_payload, first_observed_at, stable_at,
                 discovery_id, status)
            VALUES
                ('watch:v1-manual', 'Manual', 1, 10, 11,
                 'folder-inventory-v1:' || repeat('1', 64), '{}'::jsonb,
                 'candidate-snapshot-v1:' || repeat('1', 64), '{}'::jsonb,
                 clock_timestamp(), clock_timestamp(),
                 'discovery:v1-manual', 'active'),
                ('watch:v1-auto', 'Automatic', 2, 20, 21,
                 'folder-inventory-v1:' || repeat('2', 64), '{}'::jsonb,
                 'candidate-snapshot-v1:' || repeat('2', 64), '{}'::jsonb,
                 clock_timestamp(), clock_timestamp(),
                 'discovery:v1-auto', 'active');
            INSERT INTO run_operations
                (run_id, operation_id, operation_kind)
            VALUES
                ('run:v1-manual', 'operation:v1-manual', 'manual_apply'),
                ('run:v1-auto', 'operation:v1-auto', 'automatic_apply');
            INSERT INTO interactions
                (interaction_id, run_id, kind, idempotency_key,
                 request_hash, expected_plan_hash, session_revision,
                 status)
            VALUES
                ('interaction:v1-manual', 'run:v1-manual', 'revision',
                 'idempotency:v1-manual',
                 'sha256:' || repeat('c', 64),
                 'sha256:' || repeat('a', 64), 0, 'active'),
                ('interaction:v1-auto', 'run:v1-auto', 'question',
                 'idempotency:v1-auto',
                 'sha256:' || repeat('d', 64),
                 'sha256:' || repeat('b', 64), 0, 'active');
            INSERT INTO notification_outbox
                (notification_id, dedupe_key, notification_type,
                 schema_version, payload_json)
            VALUES
                ('notification:v1-manual',
                 'plan_ready:sha256:' || repeat('a', 64),
                 'plan_ready', 1, '{}'::jsonb),
                ('notification:v1-auto',
                 'plan_ready:sha256:' || repeat('b', 64),
                 'plan_ready', 1, '{}'::jsonb);
            """
        )

        for migration in MIGRATIONS[40:]:
            connection.execute(migration.sql)

        assert connection.execute(
            """
            SELECT run_id, mode, classification_reason, effect_kind,
                   effect_plan_hash, effect_policy, operation_id
            FROM run_lifecycle_controls_v2
            WHERE run_id IN ('run:v1-manual', 'run:v1-auto')
            ORDER BY run_id
            """
        ).fetchall() == [
            (
                "run:v1-auto",
                "legacy_read_only",
                "legacy_v1_snapshot",
                None,
                None,
                None,
                None,
            ),
            (
                "run:v1-manual",
                "legacy_read_only",
                "legacy_v1_snapshot",
                None,
                None,
                None,
                None,
            ),
        ]
        assert connection.execute(
            """
            SELECT run_id, status FROM runs
            WHERE run_id IN ('run:v1-manual', 'run:v1-auto')
            ORDER BY run_id
            """
        ).fetchall() == [
            ("run:v1-auto", "completed"),
            ("run:v1-manual", "completed"),
        ]
        assert connection.execute(
            """
            SELECT notification_id, state FROM notification_outbox
            WHERE notification_id LIKE 'notification:v1-%'
            ORDER BY notification_id
            """
        ).fetchall() == [
            ("notification:v1-auto", "cancelled"),
            ("notification:v1-manual", "cancelled"),
        ]
        assert connection.execute(
            """
            SELECT count(*) FROM run_operations
            WHERE run_id IN ('run:v1-manual', 'run:v1-auto')
            """
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT interaction_id, status, result->>'error_code'
            FROM interactions
            WHERE run_id IN ('run:v1-manual', 'run:v1-auto')
            ORDER BY interaction_id
            """
        ).fetchall() == [
            (
                "interaction:v1-auto",
                "failed",
                "legacy_effect_superseded",
            ),
            (
                "interaction:v1-manual",
                "failed",
                "legacy_effect_superseded",
            ),
        ]
        assert connection.execute(
            """
            SELECT count(*) FROM handled_folder_inventories_v2
            WHERE run_id IN ('run:v1-manual', 'run:v1-auto')
            """
        ).fetchone() == (0,)
        assert connection.execute(
            f"""
            SELECT r.run_id, ({RUN_DELETION_READY_SQL})
            FROM runs AS r
            JOIN discoveries AS d USING (discovery_id)
            WHERE r.run_id IN ('run:v1-manual', 'run:v1-auto')
            ORDER BY r.run_id
            """
        ).fetchall() == [
            ("run:v1-auto", True),
            ("run:v1-manual", True),
        ]
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()


@pytest.mark.postgres
def test_m14_6_terminalizes_orphan_manual_approval_claim() -> None:
    schema = "m14_6_orphan_claim_" + uuid.uuid4().hex
    connection = psycopg.connect(_dsn(), autocommit=True)
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        for migration in MIGRATIONS[:42]:
            connection.execute(migration.sql)
        plan_hash = "sha256:" + "a" * 64
        inventory_id = "folder-inventory-v2:" + "b" * 64
        connection.execute(
            """
            INSERT INTO config_revisions
                (revision_id, revision, payload, created_at)
            VALUES ('config:orphan-claim', 1,
                    '{"apply_policy":"manual"}'::jsonb,
                    clock_timestamp());
            INSERT INTO watch_states
                (watch_id, config_revision, fence, work_type,
                 settle_interval_seconds, semantic_v2)
            VALUES ('watch:orphan-claim', 1, 1, 'anime', 1, true);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES ('discovery:orphan-claim', 'watch:orphan-claim', 1,
                    'candidate-snapshot-v2:' || repeat('c', 64),
                    '{}'::jsonb, 'anime', clock_timestamp(), 'Orphan',
                    'folder-generation-v2:' || repeat('d', 64),
                    'folder-inventory-v2:' || repeat('b', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:orphan-claim', 'discovery:orphan-claim', 1,
                    'anime', 'capability:orphan-claim', 'applying');
            INSERT INTO jobs (job_id, run_id, status)
            VALUES ('job:orphan-claim', 'run:orphan-claim', 'running');
            INSERT INTO watch_folder_observations
                (watch_id, folder_name, config_revision,
                 inventory_id, inventory_payload, snapshot_id,
                 snapshot_payload, first_observed_at, stable_at,
                 discovery_id, status)
            VALUES ('watch:orphan-claim', 'Orphan', 1,
                    'folder-inventory-v2:' || repeat('b', 64), '{}'::jsonb,
                    'candidate-snapshot-v2:' || repeat('c', 64),
                    '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                    'discovery:orphan-claim', 'active');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:orphan-claim', 1,
                    'sha256:' || repeat('a', 64), 'initial');
            INSERT INTO plan_heads (run_id, version, plan_hash)
            VALUES ('run:orphan-claim', 1,
                    'sha256:' || repeat('a', 64));
            INSERT INTO run_lifecycle_controls_v2
                (run_id, mode, classification_reason, revision,
                 effect_kind, effect_plan_hash, effect_policy,
                 handoff_event_sequence)
            VALUES ('run:orphan-claim', 'forward_v2', 'test_orphan', 1,
                    'media_move', 'sha256:' || repeat('a', 64),
                    'manual', 1);
            INSERT INTO approvals
                (approval_id, run_id, plan_hash, scope, expires_at,
                 canonical_record)
            VALUES ('approval:orphan-claim', 'run:orphan-claim',
                    'sha256:' || repeat('a', 64),
                    'apply', clock_timestamp() + interval '1 hour', '\\x00');
            INSERT INTO approval_claims (approval_id, run_id, plan_hash)
            VALUES ('approval:orphan-claim', 'run:orphan-claim',
                    'sha256:' || repeat('a', 64));
            INSERT INTO run_operations
                (run_id, operation_id, operation_kind)
            VALUES ('run:orphan-claim', 'legacy-operation:orphan-claim',
                    'manual_apply');
            INSERT INTO notification_outbox
                (notification_id, dedupe_key, notification_type,
                 schema_version, payload_json)
            VALUES ('notification:orphan-claim',
                    'plan_ready:sha256:' || repeat('a', 64),
                    'plan_ready', 1, '{}'::jsonb);
            INSERT INTO run_states
                (run_id, event_sequence, phase, runtime_status,
                 model_turns, model_tokens, tool_calls, failures,
                 plan_hash, deadline_at, projection_schema,
                 projection_payload)
            VALUES ('run:orphan-claim', 1, 'awaiting_approval', 'stopped',
                    0, 0, 0, 0, 'sha256:' || repeat('a', 64),
                    clock_timestamp() + interval '1 hour',
                    'test-v1', '{}'::jsonb);
            INSERT INTO discoveries
                (discovery_id, watch_id, config_revision, snapshot_id,
                 snapshot_payload, work_type, discovered_at, source_folder,
                 folder_generation_id, inventory_id)
            VALUES ('discovery:legacy-subtitle-request',
                    'watch:orphan-claim', 1,
                    'candidate-snapshot-v1:' || repeat('d', 64),
                    '{}'::jsonb, 'anime', clock_timestamp(),
                    'LegacySubtitleRequest',
                    'folder-generation-v2:' || repeat('e', 64),
                    'folder-inventory-v2:' || repeat('f', 64));
            INSERT INTO runs
                (run_id, discovery_id, config_revision, work_type,
                 source_capability, status)
            VALUES ('run:legacy-subtitle-request',
                    'discovery:legacy-subtitle-request', 1, 'anime',
                    'source:legacy-subtitle-request', 'applying');
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, plan_kind)
            VALUES ('run:legacy-subtitle-request', 1,
                    'sha256:' || repeat('b', 64), 'initial');
            INSERT INTO run_lifecycle_controls_v2
                (run_id, mode, classification_reason)
            VALUES ('run:legacy-subtitle-request', 'legacy_read_only',
                    'test_legacy_request');
            INSERT INTO subtitle_acquisition_requests
                (run_id, plan_hash, config_revision, policy, status)
            VALUES ('run:legacy-subtitle-request',
                    'sha256:' || repeat('b', 64), 1,
                    'automatic', 'planned');
            """
        )

        connection.execute(MIGRATIONS[42].sql)

        assert connection.execute(
            """
            SELECT outcome, reason_code, source_disposition
            FROM planning_terminal_results_v2
            WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == (
            "migration_quarantine",
            "orphan_approval_claim",
            "preserve",
        )
        assert connection.execute(
            """
            SELECT status FROM runs WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == ("failed",)
        assert connection.execute(
            """
            SELECT status FROM jobs WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == ("completed",)
        assert connection.execute(
            """
            SELECT phase, runtime_status,
                   projection_payload->>'failure_code'
            FROM run_states WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == ("failed", "failed", "orphan_approval_claim")
        assert connection.execute(
            """
            SELECT request_kind, state, expected_inventory_id
            FROM generation_requests_v2
            WHERE origin_run_id = 'run:orphan-claim'
            """
        ).fetchone() == ("planning_rescan", "queued", inventory_id)
        assert connection.execute(
            """
            SELECT terminal_status FROM handled_folder_inventories_v2
            WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == ("agent_failed",)
        assert connection.execute(
            """
            SELECT state FROM notification_outbox
            WHERE notification_id = 'notification:orphan-claim'
            """
        ).fetchone() == ("cancelled",)
        assert connection.execute(
            """
            SELECT count(*) FROM approval_claims
            WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT count(*) FROM run_operations
            WHERE run_id = 'run:orphan-claim'
            """
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT status, failure_code
            FROM subtitle_acquisition_requests
            WHERE run_id = 'run:legacy-subtitle-request'
            """
        ).fetchone() == ("blocked", "legacy_read_only")
        assert connection.execute(
            f"""
            SELECT ({RUN_DELETION_READY_SQL})
            FROM runs AS r
            JOIN discoveries AS d USING (discovery_id)
            WHERE r.run_id = 'run:orphan-claim'
            """
        ).fetchone() == (True,)
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
        )
        connection.close()
