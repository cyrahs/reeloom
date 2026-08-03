"""Configuration-bound lifecycle for the single notification sender."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from reeloom.server.config import ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.notification_outbox import (
    NotificationDeliveryWorker,
    NotificationSender,
    PostgresNotificationOutbox,
    RetryPolicy,
)
from reeloom.server.notifications import TelegramTestNotification


class ConfigHead(Protocol):
    def head(self) -> ConfigRevision | None: ...


class SecretReader(Protocol):
    def load(self, reference: str) -> bytes: ...


class SenderLease(NotificationSender, Protocol):
    def close(self) -> None: ...


SenderFactory = Callable[[str, str], SenderLease]


class TelegramTestQueue:
    def __init__(
        self,
        *,
        configs: ConfigHead,
        outbox: PostgresNotificationOutbox,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._configs = configs
        self._outbox = outbox
        self._clock = clock
        self._id_factory = id_factory

    def enqueue(self, idempotency_key: str) -> dict[str, object]:
        config = self._configs.head()
        if config is None or not config.telegram.secret_ref:
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        result = self._outbox.enqueue(
            notification_id=f"notification-{self._id_factory()}",
            dedupe_key=f"test:{digest}",
            payload=TelegramTestNotification(),
            available_at=self._clock(),
        )
        return {
            "notification_id": result.notification_id,
            "state": "queued",
        }


class ConfiguredNotificationDelivery:
    """Rotate a redacted sender by config revision and drain one row per cycle."""

    def __init__(
        self,
        *,
        configs: ConfigHead,
        secrets: SecretReader,
        outbox: PostgresNotificationOutbox,
        sender_factory: SenderFactory,
        worker_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._configs = configs
        self._secrets = secrets
        self._outbox = outbox
        self._sender_factory = sender_factory
        self._worker_id = worker_id
        self._clock = clock
        self._revision: int | None = None
        self._sender: SenderLease | None = None

    def start(self) -> int:
        return self._outbox.recover_expired_leases(
            now=self._clock(),
            max_attempts=RetryPolicy().max_attempts,
        )

    def run_once(self) -> bool:
        config = self._configs.head()
        if config is None or not config.telegram.secret_ref:
            self._rotate(None)
            return False
        if self._revision != config.revision:
            token = self._secrets.load(config.telegram.secret_ref).decode(
                "utf-8",
                errors="strict",
            )
            self._rotate(
                self._sender_factory(token, config.telegram.chat_id),
                revision=config.revision,
            )
        assert self._sender is not None
        return NotificationDeliveryWorker(
            repository=self._outbox,
            sender=self._sender,
            worker_id=self._worker_id,
            clock=self._clock,
        ).run_once()

    def close(self) -> None:
        self._rotate(None)

    def _rotate(
        self,
        sender: SenderLease | None,
        *,
        revision: int | None = None,
    ) -> None:
        previous = self._sender
        self._sender = sender
        self._revision = revision
        if previous is not None:
            previous.close()
