from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.notification_outbox import (
    ClaimedNotification,
    DeliveryErrorCode,
    DeliveryResult,
    NotificationDeliveryWorker,
    NotificationOutboxError,
    OutboxState,
    RetryPolicy,
    decode_notification_payload,
    encode_notification_payload,
)
from reeloom.server.notifications import (
    ArchiveCompletedNotification,
    AttentionKind,
    AttentionNotification,
    FolderOutcome,
    NotificationSubject,
    PlanReadyNotification,
    TelegramTestNotification,
    TmdbPosterRef,
)


def _subject() -> NotificationSubject:
    return NotificationSubject(
        title="葬送的芙莉莲",
        year=2023,
        work_type=TmdbWorkType.ANIME,
        tmdb_id=209867,
        poster=TmdbPosterRef("/poster.jpg"),
    )


@pytest.mark.parametrize(
    "payload",
    (
        PlanReadyNotification(
            subject=_subject(),
            scope_label="S01E01-E04",
            video_count=4,
            subtitle_count=4,
            unmapped_count=1,
            plan_hash="sha256:" + "a" * 64,
        ),
        ArchiveCompletedNotification(
            subject=_subject(),
            applied_count=8,
            unmapped_count=1,
            folder_outcome=FolderOutcome.ARCHIVED,
            transaction_id="txn-7f31",
        ),
        AttentionNotification(
            subject=_subject(),
            kind=AttentionKind.TARGET_EXISTS,
            event_id="evt-a8c2",
        ),
        TelegramTestNotification(),
    ),
)
def test_strict_codec_round_trips_closed_payloads(payload: object) -> None:
    encoded = encode_notification_payload(payload)  # type: ignore[arg-type]

    assert decode_notification_payload(
        notification_type=encoded.notification_type.value,
        schema_version=encoded.schema_version,
        payload_json=encoded.payload_json,
    ) == payload
    assert json.dumps(
        json.loads(encoded.payload_json),
        separators=(",", ":"),
        sort_keys=True,
    ) == encoded.payload_json


@pytest.mark.parametrize(
    ("notification_type", "schema_version", "payload", "code"),
    (
        ("email", 1, {}, "invalid_notification_type"),
        ("test", 2, {}, "unsupported_schema_version"),
        ("test", 1, {"text": "injected"}, "invalid_payload"),
        ("test", 1, [], "invalid_payload"),
        ("test", 1, "not-json", "invalid_payload"),
        (
            "attention_required",
            1,
            {"subject": {}, "kind": "invented", "event_id": "evt-1"},
            "invalid_payload",
        ),
    ),
)
def test_codec_rejects_unknown_schema_fields_and_variants(
    notification_type: object,
    schema_version: object,
    payload: object,
    code: str,
) -> None:
    with pytest.raises(NotificationOutboxError) as raised:
        decode_notification_payload(
            notification_type=notification_type,
            schema_version=schema_version,
            payload_json=payload,
        )

    assert raised.value.code == code


def test_delivery_result_is_closed_and_rate_limit_is_bounded() -> None:
    assert DeliveryResult.sent(123).message_id == 123
    assert DeliveryResult.failed(
        DeliveryErrorCode.RATE_LIMITED,
        retry_after_seconds=60,
    ).retry_after_seconds == 60

    with pytest.raises(NotificationOutboxError):
        DeliveryResult()
    with pytest.raises(NotificationOutboxError):
        DeliveryResult.failed(DeliveryErrorCode.RATE_LIMITED)
    with pytest.raises(NotificationOutboxError):
        DeliveryResult.failed(
            DeliveryErrorCode.CLIENT_ERROR,
            retry_after_seconds=1,
        )


def test_retry_policy_honors_retry_after_and_bounds_exponential_jitter() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        base_delay_seconds=5,
        max_delay_seconds=20,
    )
    transient = DeliveryResult.failed(DeliveryErrorCode.CONNECTION)
    limited = DeliveryResult.failed(
        DeliveryErrorCode.RATE_LIMITED,
        retry_after_seconds=17,
    )

    assert policy.delay_seconds(
        attempt_count=1,
        result=transient,
        jitter=0.0,
    ) == 5
    assert policy.delay_seconds(
        attempt_count=3,
        result=transient,
        jitter=1.0,
    ) == 20
    assert policy.delay_seconds(
        attempt_count=1,
        result=limited,
        jitter=0.5,
    ) == 17


def test_timestamp_fixture_is_timezone_aware() -> None:
    assert datetime(2026, 8, 3, tzinfo=UTC).utcoffset() is not None


def test_worker_sends_only_after_claim_transaction_and_bounds_attempts() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    calls: list[str] = []

    class Repository:
        attempts = 0

        def recover_expired_leases(self, **_: object) -> int:
            return 0

        def claim(self, **_: object) -> object:
            self.attempts += 1
            calls.append("claim_committed")
            return ClaimedNotification(
                notification_id="notification-1",
                dedupe_key="test-1",
                attempt_count=self.attempts,
                lease_owner="worker-1",
                lease_expires_at=now + timedelta(seconds=30),
                payload=TelegramTestNotification(),
            )

        def settle_sent(self, **_: object) -> None:
            raise AssertionError("unexpected success")

        def settle_failed(self, **values: object) -> None:
            calls.append(f"settled:{values['state']}")

    class Sender:
        def send(self, _: object) -> DeliveryResult:
            assert calls == ["claim_committed"]
            calls.append("send")
            return DeliveryResult.failed(DeliveryErrorCode.TIMEOUT)

    repository = Repository()
    worker = NotificationDeliveryWorker(
        repository=repository,  # type: ignore[arg-type]
        sender=Sender(),  # type: ignore[arg-type]
        worker_id="worker-1",
        retry_policy=RetryPolicy(max_attempts=1),
        clock=lambda: now,
    )

    assert worker.run_once()
    assert calls == ["claim_committed", "send", f"settled:{OutboxState.DEAD}"]
