"""Durable, network-free notification outbox and delivery state machine."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from psycopg_pool import ConnectionPool

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.notifications import (
    ArchiveCompletedNotification,
    AttentionKind,
    AttentionNotification,
    FolderOutcome,
    NotificationContractError,
    NotificationPayload,
    NotificationSubject,
    NotificationType,
    PlanReadyNotification,
    RenderedNotification,
    TelegramTestNotification,
    TmdbPosterRef,
    render_notification,
)

_SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEDUPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_MESSAGE_ID = (1 << 63) - 1


class NotificationOutboxError(ValueError):
    """A bounded outbox contract or state-transition failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OutboxState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SENT = "sent"
    DEAD = "dead"
    CANCELLED = "cancelled"


class DeliveryErrorCode(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    CLIENT_ERROR = "client_error"
    INVALID_RESPONSE = "invalid_response"
    INVALID_PAYLOAD = "invalid_payload"
    LEASE_EXPIRED = "lease_expired"

    @property
    def retryable(self) -> bool:
        return self in {
            DeliveryErrorCode.CONNECTION,
            DeliveryErrorCode.TIMEOUT,
            DeliveryErrorCode.SERVER_ERROR,
            DeliveryErrorCode.RATE_LIMITED,
            DeliveryErrorCode.LEASE_EXPIRED,
        }


@dataclass(frozen=True, slots=True)
class EncodedNotification:
    notification_type: NotificationType
    schema_version: int
    payload_json: str


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    notification_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    notification_id: str
    dedupe_key: str
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime
    payload: NotificationPayload


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    notification_id: str
    dedupe_key: str
    notification_type: NotificationType
    state: OutboxState
    attempt_count: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    telegram_message_id: int | None
    last_error_code: DeliveryErrorCode | None


@dataclass(frozen=True, slots=True)
class OutboxStats:
    pending: int
    dead: int


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    message_id: int | None = None
    error_code: DeliveryErrorCode | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        successful = self.message_id is not None
        if successful == (self.error_code is not None):
            raise NotificationOutboxError("invalid_delivery_result")
        if successful and (
            type(self.message_id) is not int
            or not 1 <= self.message_id <= _MAX_MESSAGE_ID
        ):
            raise NotificationOutboxError("invalid_message_id")
        if self.error_code is DeliveryErrorCode.RATE_LIMITED:
            if (
                type(self.retry_after_seconds) is not int
                or not 1 <= self.retry_after_seconds <= 86_400
            ):
                raise NotificationOutboxError("invalid_retry_after")
        elif self.retry_after_seconds is not None:
            raise NotificationOutboxError("invalid_retry_after")

    @classmethod
    def sent(cls, message_id: int) -> DeliveryResult:
        return cls(message_id=message_id)

    @classmethod
    def failed(
        cls,
        error_code: DeliveryErrorCode,
        *,
        retry_after_seconds: int | None = None,
    ) -> DeliveryResult:
        return cls(
            error_code=error_code,
            retry_after_seconds=retry_after_seconds,
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 6
    base_delay_seconds: int = 5
    max_delay_seconds: int = 300

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 100:
            raise NotificationOutboxError("invalid_retry_policy")
        if not 1 <= self.base_delay_seconds <= self.max_delay_seconds:
            raise NotificationOutboxError("invalid_retry_policy")

    def delay_seconds(
        self,
        *,
        attempt_count: int,
        result: DeliveryResult,
        jitter: float,
    ) -> int:
        if not 1 <= attempt_count <= 100:
            raise NotificationOutboxError("invalid_attempt_count")
        if not 0.0 <= jitter <= 1.0:
            raise NotificationOutboxError("invalid_jitter")
        if result.error_code is DeliveryErrorCode.RATE_LIMITED:
            assert result.retry_after_seconds is not None
            return result.retry_after_seconds
        base = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, attempt_count - 1)),
        )
        return min(
            self.max_delay_seconds,
            base + math.floor(base * 0.2 * jitter),
        )


class NotificationSender(Protocol):
    def send(self, notification: RenderedNotification) -> DeliveryResult: ...


def encode_notification_payload(
    payload: NotificationPayload,
) -> EncodedNotification:
    if type(payload) is PlanReadyNotification:
        notification_type = NotificationType.PLAN_READY
        value = {
            "subject": _subject_to_value(payload.subject),
            "scope_label": payload.scope_label,
            "video_count": payload.video_count,
            "subtitle_count": payload.subtitle_count,
            "unmapped_count": payload.unmapped_count,
            "plan_hash": payload.plan_hash,
        }
    elif type(payload) is ArchiveCompletedNotification:
        notification_type = NotificationType.ARCHIVE_COMPLETED
        value = {
            "subject": _subject_to_value(payload.subject),
            "applied_count": payload.applied_count,
            "unmapped_count": payload.unmapped_count,
            "folder_outcome": payload.folder_outcome.value,
            "transaction_id": payload.transaction_id,
        }
    elif type(payload) is AttentionNotification:
        notification_type = NotificationType.ATTENTION_REQUIRED
        value = {
            "subject": _subject_to_value(payload.subject),
            "kind": payload.kind.value,
            "event_id": payload.event_id,
        }
    elif type(payload) is TelegramTestNotification:
        notification_type = NotificationType.TEST
        value = {}
    else:
        raise NotificationOutboxError("unsupported_notification_type")
    return EncodedNotification(
        notification_type=notification_type,
        schema_version=_SCHEMA_VERSION,
        payload_json=json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def decode_notification_payload(
    *,
    notification_type: object,
    schema_version: object,
    payload_json: object,
) -> NotificationPayload:
    try:
        kind = NotificationType(notification_type)
    except (TypeError, ValueError):
        raise NotificationOutboxError("invalid_notification_type") from None
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise NotificationOutboxError("unsupported_schema_version")
    try:
        value = (
            json.loads(payload_json)
            if isinstance(payload_json, str)
            else payload_json
        )
    except (TypeError, ValueError):
        raise NotificationOutboxError("invalid_payload") from None
    try:
        if kind is NotificationType.PLAN_READY:
            item = _exact_dict(
                value,
                {
                    "subject",
                    "scope_label",
                    "video_count",
                    "subtitle_count",
                    "unmapped_count",
                    "plan_hash",
                },
            )
            return PlanReadyNotification(
                subject=_subject_from_value(item["subject"]),
                scope_label=item["scope_label"],
                video_count=item["video_count"],
                subtitle_count=item["subtitle_count"],
                unmapped_count=item["unmapped_count"],
                plan_hash=item["plan_hash"],
            )
        if kind is NotificationType.ARCHIVE_COMPLETED:
            item = _exact_dict(
                value,
                {
                    "subject",
                    "applied_count",
                    "unmapped_count",
                    "folder_outcome",
                    "transaction_id",
                },
            )
            return ArchiveCompletedNotification(
                subject=_subject_from_value(item["subject"]),
                applied_count=item["applied_count"],
                unmapped_count=item["unmapped_count"],
                folder_outcome=FolderOutcome(item["folder_outcome"]),
                transaction_id=item["transaction_id"],
            )
        if kind is NotificationType.ATTENTION_REQUIRED:
            item = _exact_dict(value, {"subject", "kind", "event_id"})
            return AttentionNotification(
                subject=_subject_from_value(item["subject"]),
                kind=AttentionKind(item["kind"]),
                event_id=item["event_id"],
            )
        _exact_dict(value, set())
        return TelegramTestNotification()
    except (
        KeyError,
        TypeError,
        ValueError,
        NotificationContractError,
    ):
        raise NotificationOutboxError("invalid_payload") from None


class PostgresNotificationOutbox:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def enqueue(
        self,
        *,
        notification_id: str,
        dedupe_key: str,
        payload: NotificationPayload,
        available_at: datetime,
    ) -> EnqueueResult:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    return self.enqueue_in_transaction(
                        connection=connection,
                        notification_id=notification_id,
                        dedupe_key=dedupe_key,
                        payload=payload,
                        available_at=available_at,
                    )
        except (NotificationOutboxError, ServerError):
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def enqueue_in_transaction(
        self,
        *,
        connection: Any,
        notification_id: str,
        dedupe_key: str,
        payload: NotificationPayload,
        available_at: datetime,
    ) -> EnqueueResult:
        """Insert using the caller's durable-fact transaction."""

        _validate_id(notification_id, "invalid_notification_id")
        if (
            not isinstance(dedupe_key, str)
            or _DEDUPE_PATTERN.fullmatch(dedupe_key) is None
        ):
            raise NotificationOutboxError("invalid_dedupe_key")
        _validate_time(available_at)
        encoded = encode_notification_payload(payload)
        try:
            row = connection.execute(
                """
                INSERT INTO notification_outbox (
                    notification_id, dedupe_key, notification_type,
                    schema_version, payload_json, available_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING notification_id
                """,
                (
                    notification_id,
                    dedupe_key,
                    encoded.notification_type.value,
                    encoded.schema_version,
                    encoded.payload_json,
                    available_at,
                ),
            ).fetchone()
            if row is not None:
                return EnqueueResult(str(row[0]), True)
            existing = connection.execute(
                """
                SELECT notification_id, notification_type,
                       schema_version, payload_json = %s::jsonb
                FROM notification_outbox
                WHERE dedupe_key = %s
                """,
                (encoded.payload_json, dedupe_key),
            ).fetchone()
            if (
                existing is None
                or str(existing[1]) != encoded.notification_type.value
                or int(existing[2]) != encoded.schema_version
                or not bool(existing[3])
            ):
                raise NotificationOutboxError("dedupe_conflict")
            return EnqueueResult(str(existing[0]), False)
        except NotificationOutboxError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ClaimedNotification | None:
        _validate_id(worker_id, "invalid_worker_id")
        _validate_time(now)
        if not timedelta(seconds=1) <= lease_for <= timedelta(minutes=10):
            raise NotificationOutboxError("invalid_lease_duration")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    # Revalidate old and newly projected approval notices at
                    # the final delivery boundary. A pre-cutover writer or a
                    # crash between lifecycle settlement and migration must
                    # not resurrect a stale "waiting for approval" message.
                    connection.execute(
                        """
                        UPDATE notification_outbox AS notification
                        SET state = 'cancelled', lease_owner = NULL,
                            lease_expires_at = NULL,
                            last_error_code = NULL, updated_at = %s
                        WHERE notification.notification_type = 'plan_ready'
                          AND notification.state IN (
                              'queued', 'retry_wait'
                          )
                          AND EXISTS (
                              SELECT 1
                              FROM effect_plan_bindings_v2 AS binding
                              JOIN run_lifecycle_controls_v2 AS control
                                ON control.run_id = binding.run_id
                              LEFT JOIN execution_operations_v2 AS operation
                                ON operation.operation_id =
                                   control.operation_id
                              LEFT JOIN planning_terminal_results_v2 AS terminal
                                ON terminal.run_id = control.run_id
                              WHERE binding.plan_hash =
                                    notification.payload_json->>'plan_hash'
                                AND (
                                    control.mode = 'legacy_read_only'
                                    OR control.effect_policy
                                       IS DISTINCT FROM 'manual'
                                    OR control.effect_plan_hash
                                       IS DISTINCT FROM binding.plan_hash
                                    OR control.operation_id IS NOT NULL
                                    OR terminal.run_id IS NOT NULL
                                    OR operation.status IN (
                                        'completed', 'partial', 'stale',
                                        'collision', 'unsafe', 'unavailable',
                                        'superseded'
                                    )
                                )
                          )
                        """,
                        (now,),
                    )
                    row = connection.execute(
                        """
                        SELECT notification_id, dedupe_key,
                               notification_type, schema_version, payload_json,
                               attempt_count
                        FROM notification_outbox
                        WHERE state IN ('queued', 'retry_wait')
                          AND available_at <= %s
                        ORDER BY available_at, created_at, notification_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (now,),
                    ).fetchone()
                    if row is None:
                        return None
                    try:
                        payload = decode_notification_payload(
                            notification_type=row[2],
                            schema_version=row[3],
                            payload_json=row[4],
                        )
                    except NotificationOutboxError:
                        connection.execute(
                            """
                            UPDATE notification_outbox
                            SET state = 'dead',
                                last_error_code = 'invalid_payload',
                                updated_at = %s
                            WHERE notification_id = %s
                            """,
                            (now, row[0]),
                        )
                        return None
                    attempt_count = int(row[5]) + 1
                    lease_expires_at = now + lease_for
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            last_error_code = NULL, updated_at = %s
                        WHERE notification_id = %s
                        """,
                        (
                            attempt_count,
                            worker_id,
                            lease_expires_at,
                            now,
                            row[0],
                        ),
                    )
                    return ClaimedNotification(
                        notification_id=str(row[0]),
                        dedupe_key=str(row[1]),
                        attempt_count=attempt_count,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        payload=payload,
                    )
        except NotificationOutboxError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settle_sent(
        self,
        *,
        claim: ClaimedNotification,
        message_id: int,
        now: datetime,
    ) -> None:
        result = DeliveryResult.sent(message_id)
        _validate_time(now)
        self._settle(
            claim=claim,
            state=OutboxState.SENT,
            now=now,
            available_at=now,
            message_id=result.message_id,
            error_code=None,
        )

    def settle_failed(
        self,
        *,
        claim: ClaimedNotification,
        state: OutboxState,
        error_code: DeliveryErrorCode,
        available_at: datetime,
        now: datetime,
    ) -> None:
        if state not in {OutboxState.RETRY_WAIT, OutboxState.DEAD}:
            raise NotificationOutboxError("invalid_settlement_state")
        _validate_time(now)
        _validate_time(available_at)
        self._settle(
            claim=claim,
            state=state,
            now=now,
            available_at=available_at,
            message_id=None,
            error_code=error_code,
        )

    def _settle(
        self,
        *,
        claim: ClaimedNotification,
        state: OutboxState,
        now: datetime,
        available_at: datetime,
        message_id: int | None,
        error_code: DeliveryErrorCode | None,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE notification_outbox
                        SET state = %s, available_at = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            telegram_message_id = %s,
                            last_error_code = %s, updated_at = %s
                        WHERE notification_id = %s
                          AND state = 'leased'
                          AND lease_owner = %s
                          AND attempt_count = %s
                        RETURNING notification_id
                        """,
                        (
                            state.value,
                            available_at,
                            message_id,
                            None if error_code is None else error_code.value,
                            now,
                            claim.notification_id,
                            claim.lease_owner,
                            claim.attempt_count,
                        ),
                    ).fetchone()
                    if row is None:
                        raise NotificationOutboxError("stale_lease")
        except NotificationOutboxError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def recover_expired_leases(
        self,
        *,
        now: datetime,
        max_attempts: int,
    ) -> int:
        _validate_time(now)
        if not 1 <= max_attempts <= 100:
            raise NotificationOutboxError("invalid_retry_policy")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        """
                        UPDATE notification_outbox
                        SET state = CASE
                                WHEN attempt_count >= %s THEN 'dead'
                                ELSE 'retry_wait'
                            END,
                            available_at = %s,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            last_error_code = 'lease_expired',
                            updated_at = %s
                        WHERE state = 'leased'
                          AND lease_expires_at <= %s
                        RETURNING notification_id
                        """,
                        (max_attempts, now, now, now),
                    ).fetchall()
                    return len(rows)
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def get(self, notification_id: str) -> OutboxRecord:
        _validate_id(notification_id, "invalid_notification_id")
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT notification_id, dedupe_key, notification_type,
                           state, attempt_count, available_at, lease_owner,
                           lease_expires_at, telegram_message_id,
                           last_error_code
                    FROM notification_outbox
                    WHERE notification_id = %s
                    """,
                    (notification_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise NotificationOutboxError("notification_not_found")
        return OutboxRecord(
            notification_id=str(row[0]),
            dedupe_key=str(row[1]),
            notification_type=NotificationType(row[2]),
            state=OutboxState(row[3]),
            attempt_count=int(row[4]),
            available_at=row[5],
            lease_owner=None if row[6] is None else str(row[6]),
            lease_expires_at=row[7],
            telegram_message_id=(
                None if row[8] is None else int(row[8])
            ),
            last_error_code=(
                None if row[9] is None else DeliveryErrorCode(row[9])
            ),
        )

    def stats(self) -> OutboxStats:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        count(*) FILTER (
                            WHERE state IN ('queued', 'leased', 'retry_wait')
                        ),
                        count(*) FILTER (WHERE state = 'dead')
                    FROM notification_outbox
                    """
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
        return OutboxStats(pending=int(row[0]), dead=int(row[1]))


class NotificationDeliveryWorker:
    """Claims briefly, sends without a DB transaction, then fences settlement."""

    def __init__(
        self,
        *,
        repository: PostgresNotificationOutbox,
        sender: NotificationSender,
        worker_id: str,
        retry_policy: RetryPolicy | None = None,
        lease_for: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = lambda: 0.5,
    ) -> None:
        _validate_id(worker_id, "invalid_worker_id")
        self._repository = repository
        self._sender = sender
        self._worker_id = worker_id
        self._retry_policy = retry_policy or RetryPolicy()
        self._lease_for = lease_for
        self._clock = clock
        self._jitter = jitter

    def start(self) -> int:
        return self._repository.recover_expired_leases(
            now=self._clock(),
            max_attempts=self._retry_policy.max_attempts,
        )

    def run_once(self) -> bool:
        claim = self._repository.claim(
            worker_id=self._worker_id,
            now=self._clock(),
            lease_for=self._lease_for,
        )
        if claim is None:
            return False
        result = self._sender.send(render_notification(claim.payload))
        now = self._clock()
        if result.message_id is not None:
            self._repository.settle_sent(
                claim=claim,
                message_id=result.message_id,
                now=now,
            )
            return True
        assert result.error_code is not None
        retry = (
            result.error_code.retryable
            and claim.attempt_count < self._retry_policy.max_attempts
        )
        delay = (
            self._retry_policy.delay_seconds(
                attempt_count=claim.attempt_count,
                result=result,
                jitter=self._jitter(),
            )
            if retry
            else 0
        )
        self._repository.settle_failed(
            claim=claim,
            state=OutboxState.RETRY_WAIT if retry else OutboxState.DEAD,
            error_code=result.error_code,
            available_at=now + timedelta(seconds=delay),
            now=now,
        )
        return True


def _subject_to_value(subject: NotificationSubject) -> dict[str, object]:
    return {
        "title": subject.title,
        "year": subject.year,
        "work_type": subject.work_type.value,
        "tmdb_id": subject.tmdb_id,
        "poster": None if subject.poster is None else subject.poster.value,
    }


def _subject_from_value(value: object) -> NotificationSubject:
    item = _exact_dict(
        value,
        {"title", "year", "work_type", "tmdb_id", "poster"},
    )
    poster = item["poster"]
    return NotificationSubject(
        title=item["title"],
        year=item["year"],
        work_type=TmdbWorkType(item["work_type"]),
        tmdb_id=item["tmdb_id"],
        poster=None if poster is None else TmdbPosterRef(poster),
    )


def _exact_dict(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise NotificationOutboxError("invalid_payload")
    return value


def _validate_id(value: object, code: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise NotificationOutboxError(code)


def _validate_time(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise NotificationOutboxError("invalid_timestamp")
