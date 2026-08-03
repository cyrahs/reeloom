from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.server.database import PostgresControlPlane
from reeloom.server.notification_outbox import (
    DeliveryErrorCode,
    DeliveryResult,
    NotificationDeliveryWorker,
    NotificationOutboxError,
    OutboxState,
    PostgresNotificationOutbox,
    RetryPolicy,
)
from reeloom.server.notifications import TelegramTestNotification


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
