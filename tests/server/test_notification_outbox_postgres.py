from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.database import PostgresControlPlane
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.notification_outbox import (
    DeliveryErrorCode,
    DeliveryResult,
    NotificationDeliveryWorker,
    NotificationOutboxError,
    OutboxState,
    PostgresNotificationOutbox,
    RetryPolicy,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.notifications import (
    NotificationSubject,
    PlanReadyNotification,
    TelegramTestNotification,
)


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _enqueue(
    repository: PostgresNotificationOutbox,
    *,
    now: datetime,
) -> str:
    notification_id = _id("notification")
    result = repository.enqueue(
        notification_id=notification_id,
        dedupe_key=_id("test"),
        payload=TelegramTestNotification(),
        available_at=now,
    )
    assert result.created
    return notification_id


@pytest.mark.postgres
def test_outbox_dedupes_and_claim_has_one_concurrent_winner() -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        repository = PostgresNotificationOutbox(control.pool)
        notification_id = _id("notification")
        dedupe_key = _id("plan")

        first = repository.enqueue(
            notification_id=notification_id,
            dedupe_key=dedupe_key,
            payload=TelegramTestNotification(),
            available_at=now,
        )
        duplicate = repository.enqueue(
            notification_id=_id("notification"),
            dedupe_key=dedupe_key,
            payload=TelegramTestNotification(),
            available_at=now,
        )

        assert first.created
        assert duplicate.notification_id == notification_id
        assert not duplicate.created

        def claim(worker_id: str) -> object:
            return repository.claim(
                worker_id=worker_id,
                now=now,
                lease_for=timedelta(seconds=30),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = tuple(executor.map(claim, ("worker-a", "worker-b")))

        winners = tuple(item for item in claims if item is not None)
        assert len(winners) == 1
        assert winners[0].notification_id == notification_id
        assert repository.get(notification_id).state is OutboxState.LEASED
    finally:
        control.close()


@pytest.mark.postgres
def test_worker_retries_then_records_receipt_without_open_send_transaction() -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    current = [now]
    observations: list[tuple[str, object]] = []
    try:
        control.open()
        control.migrate()
        repository = PostgresNotificationOutbox(control.pool)
        notification_id = _enqueue(repository, now=now)

        class Sender:
            results = [
                DeliveryResult.failed(DeliveryErrorCode.CONNECTION),
                DeliveryResult.sent(4242),
            ]

            def send(self, notification: object) -> DeliveryResult:
                record = repository.get(notification_id)
                observations.append((record.state.value, notification))
                return self.results.pop(0)

        worker = NotificationDeliveryWorker(
            repository=repository,
            sender=Sender(),
            worker_id="worker-retry",
            retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay_seconds=5,
                max_delay_seconds=30,
            ),
            clock=lambda: current[0],
            jitter=lambda: 0.0,
        )

        assert worker.start() == 0
        assert worker.run_once()
        retrying = repository.get(notification_id)
        assert retrying.state is OutboxState.RETRY_WAIT
        assert retrying.attempt_count == 1
        assert retrying.last_error_code is DeliveryErrorCode.CONNECTION
        assert retrying.available_at == now + timedelta(seconds=5)

        current[0] = now + timedelta(seconds=5)
        assert worker.run_once()
        sent = repository.get(notification_id)
        assert sent.state is OutboxState.SENT
        assert sent.attempt_count == 2
        assert sent.telegram_message_id == 4242
        assert sent.last_error_code is None
        assert [item[0] for item in observations] == ["leased", "leased"]
    finally:
        control.close()


@pytest.mark.postgres
def test_restart_recovers_expired_lease_and_fences_stale_worker() -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        first_repository = PostgresNotificationOutbox(control.pool)
        notification_id = _enqueue(first_repository, now=now)
        stale = first_repository.claim(
            worker_id="worker-before-crash",
            now=now,
            lease_for=timedelta(seconds=2),
        )
        assert stale is not None

        restarted_repository = PostgresNotificationOutbox(control.pool)
        recovered_at = now + timedelta(seconds=3)
        assert restarted_repository.recover_expired_leases(
            now=recovered_at,
            max_attempts=3,
        ) == 1
        recovered = restarted_repository.get(notification_id)
        assert recovered.state is OutboxState.RETRY_WAIT
        assert recovered.last_error_code is DeliveryErrorCode.LEASE_EXPIRED

        fresh = restarted_repository.claim(
            worker_id="worker-after-restart",
            now=recovered_at,
            lease_for=timedelta(seconds=30),
        )
        assert fresh is not None
        assert fresh.attempt_count == 2
        with pytest.raises(NotificationOutboxError) as raised:
            restarted_repository.settle_sent(
                claim=stale,
                message_id=1,
                now=recovered_at,
            )
        assert raised.value.code == "stale_lease"

        restarted_repository.settle_failed(
            claim=fresh,
            state=OutboxState.DEAD,
            error_code=DeliveryErrorCode.CLIENT_ERROR,
            available_at=recovered_at,
            now=recovered_at,
        )
        dead = restarted_repository.get(notification_id)
        assert dead.state is OutboxState.DEAD
        assert dead.last_error_code is DeliveryErrorCode.CLIENT_ERROR
    finally:
        control.close()


@pytest.mark.postgres
def test_transactional_enqueue_rolls_back_with_durable_fact() -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    notification_id = _id("notification")
    try:
        control.open()
        control.migrate()
        repository = PostgresNotificationOutbox(control.pool)

        with pytest.raises(RuntimeError, match="fact failed"):
            with control.pool.connection() as connection:
                with connection.transaction():
                    repository.enqueue_in_transaction(
                        connection=connection,
                        notification_id=notification_id,
                        dedupe_key=_id("fact"),
                        payload=TelegramTestNotification(),
                        available_at=now,
                    )
                    raise RuntimeError("fact failed")

        with pytest.raises(NotificationOutboxError) as raised:
            repository.get(notification_id)
        assert raised.value.code == "notification_not_found"
    finally:
        control.close()


@pytest.mark.postgres
def test_delivery_boundary_cancels_quarantined_plan_ready() -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    suffix = uuid.uuid4().hex
    run_id = f"run-notification-quarantine-{suffix}"
    plan_hash = "sha256:" + suffix.ljust(64, "0")[:64]
    notification_id = f"notification-quarantine-{suffix}"
    try:
        control.open()
        control.migrate()
        config = PostgresConfigRepository(control.pool).head()
        assert config is not None
        watch_id = f"watch-notification-{suffix}"
        discovery_id = f"discovery-notification-{suffix}"
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
                         discovered_at)
                    VALUES (%s, %s, %s, %s, '{}'::jsonb,
                            'anime', %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        "candidate-snapshot-v2:" + "1" * 64,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'superseded')
                    """,
                    (run_id, discovery_id, config.revision, f"source-{suffix}"),
                )
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES (%s, 1, %s, 'initial')
                    """,
                    (run_id, plan_hash),
                )
                connection.execute(
                    """
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason)
                    VALUES (%s, 'legacy_read_only', 'test_quarantine')
                    """,
                    (run_id,),
                )
        repository = PostgresNotificationOutbox(control.pool)
        repository.enqueue(
            notification_id=notification_id,
            dedupe_key=f"plan_ready:{plan_hash}",
            payload=PlanReadyNotification(
                subject=NotificationSubject(
                    title="测试动画",
                    year=2026,
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=1,
                ),
                scope_label="媒体整理",
                video_count=1,
                subtitle_count=0,
                unmapped_count=0,
                plan_hash=plan_hash,
            ),
            available_at=now,
        )

        assert repository.claim(
            worker_id="notification-worker",
            now=now,
            lease_for=timedelta(seconds=30),
        ) is None
        assert repository.get(notification_id).state is OutboxState.CANCELLED
    finally:
        control.close()


@pytest.mark.postgres
@pytest.mark.parametrize("operation_status", ["authorized", "running"])
def test_delivery_boundary_cancels_plan_ready_after_authorization(
    operation_status: str,
) -> None:
    control = PostgresControlPlane(_dsn())
    now = datetime.now(UTC)
    suffix = uuid.uuid4().hex
    run_id = f"run-notification-authorized-{suffix}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    operation_id = f"operation-notification-{suffix}"
    notification_id = f"notification-authorized-{suffix}"
    try:
        control.open()
        control.migrate()
        with control.pool.connection() as connection:
            revision = int(
                connection.execute(
                    "SELECT COALESCE(max(revision), 0) FROM config_revisions"
                ).fetchone()[0]
            )
            if revision == 0:
                revision = 1
                connection.execute(
                    """
                    INSERT INTO config_revisions
                        (revision_id, revision, payload, created_at)
                    VALUES (%s, 1, '{}'::jsonb, %s)
                    """,
                    (f"config-notification-{suffix}", now),
                )
        watch_id = f"watch-notification-authorized-{suffix}"
        discovery_id = f"discovery-notification-authorized-{suffix}"
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds, semantic_v2)
                    VALUES (%s, %s, %s, 'anime', 1, true)
                    """,
                    (watch_id, revision, revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime', %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        revision,
                        "candidate-snapshot-v2:" + uuid.uuid4().hex * 2,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'applying')
                    """,
                    (run_id, discovery_id, revision, f"source-{suffix}"),
                )
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES (%s, 1, %s, 'initial')
                    """,
                    (run_id, plan_hash),
                )
        PostgresApprovalStore(control.pool).issue(approval)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO execution_operations_v2
                        (operation_id, schema_version, run_id, plan_hash,
                         approval_id, operation_kind, status, attempt_count,
                         lease_owner, lease_expires_at)
                    VALUES (%s, 2, %s, %s, %s, 'media_move', %s, 0,
                            CASE WHEN %s = 'running' THEN 'worker:test' END,
                            CASE WHEN %s = 'running'
                                 THEN %s + interval '1 minute' END)
                    """,
                    (
                        operation_id,
                        run_id,
                        plan_hash,
                        approval.approval_id,
                        operation_status,
                        operation_status,
                        operation_status,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason, revision,
                         effect_kind, effect_plan_hash, effect_policy,
                         operation_id, handoff_event_sequence)
                    VALUES (%s, 'forward_v2', 'test_authorized_notice', 1,
                            'media_move', %s, 'manual', %s, 1)
                    """,
                    (run_id, plan_hash, operation_id),
                )
        repository = PostgresNotificationOutbox(control.pool)
        repository.enqueue(
            notification_id=notification_id,
            dedupe_key=f"plan_ready:{plan_hash}",
            payload=PlanReadyNotification(
                subject=NotificationSubject(
                    title="已批准动画",
                    year=2026,
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=1,
                ),
                scope_label="媒体整理",
                video_count=1,
                subtitle_count=0,
                unmapped_count=0,
                plan_hash=plan_hash,
            ),
            available_at=now,
        )

        assert repository.claim(
            worker_id="notification-worker",
            now=now,
            lease_for=timedelta(seconds=30),
        ) is None
        assert repository.get(notification_id).state is OutboxState.CANCELLED
        with control.pool.connection() as connection:
            connection.execute(
                """
                UPDATE execution_operations_v2
                SET status = 'superseded', outcomes = '[]'::jsonb,
                    lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE operation_id = %s
                  AND status IN ('authorized', 'running')
                """,
                (operation_id,),
            )
    finally:
        control.close()
